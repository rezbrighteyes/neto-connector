# -*- coding: utf-8 -*-
import logging
import requests
from datetime import datetime, timedelta, timezone

from odoo import models, fields

_logger = logging.getLogger(__name__)

ALLOWED_STATUSES = frozenset({'New', 'Pick', 'Pack', 'Dispatched', 'Pending', 'New Backorder'})
_API_ACTION = 'GetOrder'


class NetoConnector(models.AbstractModel):
    _name = 'neto.connector'
    _description = 'Neto API Connector'

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    def _parse_neto_datetime(self, raw):
        """Return a naive UTC datetime from a Neto date string, or False."""
        if not raw:
            return False
        try:
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo:
                dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
            return dt
        except (ValueError, TypeError):
            return False

    # -------------------------------------------------------------------------
    # API
    # -------------------------------------------------------------------------

    def _fetch_orders(self, store, since_dt):
        url = f"{store.store_url.rstrip('/')}/do/WS/NetoAPI"
        headers = {
            'Content-Type': 'application/json',
            'NETOAPI_ACTION': _API_ACTION,
            'NETOAPI_KEY': store.api_key,
            'Accept': 'application/json',
        }
        payload = {
            'Filter': {
                'DateUpdatedFrom': since_dt.strftime('%Y-%m-%dT%H:%M:%S'),
                'OutputSelector': [
                    'OrderID', 'Username', 'GrandTotal', 'OrderStatus',
                    'OrderLine', 'BillingEmail', 'BillingName', 'BillingAddress',
                    'DatePlaced', 'DateUpdated',
                ],
            }
        }
        _logger.info(
            'Neto sync [%s]: POST %s  DateUpdatedFrom=%s',
            store.name, url, payload['Filter']['DateUpdatedFrom'],
        )
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=60)
            _logger.info(
                'Neto sync [%s]: HTTP %s  content-length=%s',
                store.name, response.status_code, len(response.content),
            )
            response.raise_for_status()
        except requests.exceptions.RequestException as exc:
            _logger.error(
                'Neto sync [%s]: API request failed — %s', store.name, exc
            )
            return []

        try:
            body = response.json()
        except Exception as exc:
            _logger.error(
                'Neto sync [%s]: could not parse JSON response — %s\nRaw: %s',
                store.name, exc, response.text[:500],
            )
            return []

        _logger.info(
            'Neto sync [%s]: response top-level keys=%s  Ack=%s',
            store.name,
            list(body.keys()),
            body.get('Ack') or body.get('GetOrderResponse', {}).get('Ack', 'n/a'),
        )

        if 'GetOrderResponse' in body:
            orders = body['GetOrderResponse'].get('Order', [])
        else:
            orders = body.get('Order', [])

        if isinstance(orders, dict):
            orders = [orders]
        orders = orders or []
        _logger.info('Neto sync [%s]: %d raw order(s) in response', store.name, len(orders))
        return orders

    # -------------------------------------------------------------------------
    # Partner
    # -------------------------------------------------------------------------

    def _get_or_create_partner(self, username, billing_name, billing_email, billing_address=None):
        """Find partner by neto_username or create one with full billing details."""
        Partner = self.env['res.partner'].sudo()
        partner = Partner.search([('neto_username', '=', username)], limit=1)
        if partner:
            return partner, False

        addr = billing_address or {}
        vals = {
            'neto_username': username,
            'ref': username,
            'name': billing_name if billing_name else username,
            'email': billing_email,
            'is_company': True,
            'customer_rank': 1,
            'street':  addr.get('BillStreetLine1') or addr.get('Street1') or False,
            'street2': addr.get('BillStreetLine2') or addr.get('Street2') or False,
            'city':    addr.get('BillCity') or addr.get('City') or False,
            'zip':     addr.get('BillPostCode') or addr.get('PostCode') or False,
            # mobile was removed from res.partner in Odoo 17+; phone covers both
            'phone':   addr.get('BillPhone') or addr.get('BillMobile') or addr.get('Phone') or addr.get('Mobile') or False,
        }
        country_code = addr.get('BillCountry') or addr.get('Country') or ''
        if country_code:
            country = self.env['res.country'].sudo().search(
                ['|', ('code', '=ilike', country_code),
                      ('name', '=ilike', country_code)], limit=1
            )
            if country:
                vals['country_id'] = country.id
                state_name = addr.get('BillState') or addr.get('State') or ''
                if state_name:
                    state = self.env['res.country.state'].sudo().search(
                        [('country_id', '=', country.id),
                         '|', ('code', '=ilike', state_name),
                              ('name', '=ilike', state_name)], limit=1
                    )
                    if state:
                        vals['state_id'] = state.id

        partner = Partner.create(vals)
        return partner, True

    # -------------------------------------------------------------------------
    # Order creation
    # -------------------------------------------------------------------------

    def _create_sale_order(self, order_data, partner, store):
        Order = self.env['sale.order'].sudo()
        OrderLine = self.env['sale.order.line'].sudo()
        Product = self.env['product.product'].sudo()

        date_order = (
            self._parse_neto_datetime(order_data.get('DatePlaced'))
            or fields.Datetime.now()
        )

        order = Order.create({
            'partner_id': partner.id,
            'neto_order_id': order_data.get('OrderID'),
            'date_order': date_order,
            'warehouse_id': store.warehouse_id.id,
            'company_id': store.company_id.id,
        })

        raw_lines = order_data.get('OrderLine', [])
        if isinstance(raw_lines, dict):
            raw_lines = [raw_lines]

        line_prices = {}
        for line in raw_lines:
            sku = (line.get('SKU') or line.get('Sku') or '').strip()
            if not sku:
                _logger.warning(
                    'Neto sync: order %s has a line with no SKU — skipping line',
                    order_data.get('OrderID'),
                )
                continue
            product = Product.search([('default_code', '=', sku)], limit=1)
            if not product:
                _logger.warning(
                    'Neto sync: SKU "%s" not found on order %s — skipping line',
                    sku, order_data.get('OrderID'),
                )
                continue
            neto_price = float(line.get('UnitPrice') or 0)
            try:
                OrderLine.create({
                    'order_id': order.id,
                    'product_id': product.id,
                    'product_uom_qty': float(line.get('Quantity') or 1),
                    'price_unit': neto_price,
                    'name': product.name,
                    'product_uom_id': product.uom_id.id,
                })
                line_prices[product.id] = neto_price
            except Exception as line_exc:
                _logger.warning(
                    'Neto sync: could not create line SKU=%s on order %s — %s',
                    sku, order_data.get('OrderID'), line_exc,
                )

        try:
            order.action_confirm()
            for ol in order.order_line:
                neto_price = line_prices.get(ol.product_id.id)
                if neto_price is not None and ol.price_unit != neto_price:
                    ol.sudo().write({'price_unit': neto_price})
            if date_order:
                order.sudo().write({'date_order': date_order})
        except Exception as confirm_exc:
            _logger.warning(
                'Neto sync: order %s created as draft — action_confirm() failed: %s',
                order_data.get('OrderID'), confirm_exc,
            )
        return order

    # -------------------------------------------------------------------------
    # Per-order processing
    # -------------------------------------------------------------------------

    def _process_order(self, order_data, store, synced_ids):
        """Process a single Neto order dict.

        synced_ids is a plain Python set() owned by _sync_store, passed in
        to track duplicates within the current batch without touching self.
        AbstractModel does not allow setting arbitrary instance attributes.
        """
        SyncLog = self.env['neto.sync.log'].sudo()

        order_id = order_data.get('OrderID', '')
        username = order_data.get('Username', '') or ''
        billing_email = order_data.get('BillingEmail', '') or ''
        billing_name = order_data.get('BillingName', '') or ''
        billing_address = order_data.get('BillingAddress') or {}
        grand_total = float(order_data.get('GrandTotal') or 0)
        order_status = order_data.get('OrderStatus', '') or ''
        neto_order_date = self._parse_neto_datetime(order_data.get('DatePlaced'))

        base_vals = {
            'neto_order_id': order_id,
            'neto_username': username,
            'neto_order_date': neto_order_date,
            'neto_grand_total': grand_total,
            'store_id': store.id,
        }

        try:
            # Rule 3: duplicate — silent, no log entry
            if order_id in synced_ids:
                return
            if self.env['sale.order'].sudo().search_count(
                [('neto_order_id', '=', order_id)]
            ):
                return
            synced_ids.add(order_id)

            # Rule 1: zero-value internal transfer
            if grand_total == 0:
                SyncLog.create({
                    **base_vals,
                    'state': 'skipped',
                    'skip_reason': 'Zero-value internal transfer',
                })
                return

            # Rule 2: BrightEyes replenishment
            if '@brighteyes.net.au' in billing_email.lower():
                SyncLog.create({
                    **base_vals,
                    'state': 'skipped',
                    'skip_reason': 'BrightEyes internal replenishment',
                })
                return

            # Rule 6: status filter
            if order_status not in ALLOWED_STATUSES:
                SyncLog.create({
                    **base_vals,
                    'state': 'skipped',
                    'skip_reason': f'Status not in sync list: {order_status}',
                })
                return

            partner, partner_created = self._get_or_create_partner(
                username, billing_name, billing_email, billing_address
            )
            sale_order = self._create_sale_order(order_data, partner, store)
            line_count = len(sale_order.order_line)

            if line_count == 0:
                SyncLog.create({
                    **base_vals,
                    'state': 'skipped',
                    'sale_order_id': sale_order.id,
                    'partner_id': partner.id,
                    'partner_created': partner_created,
                    'line_count': 0,
                    'skip_reason': 'No matching SKUs found in Odoo',
                })
                return

            SyncLog.create({
                **base_vals,
                'state': 'success',
                'sale_order_id': sale_order.id,
                'partner_id': partner.id,
                'partner_created': partner_created,
                'line_count': line_count,
            })

        except Exception as exc:
            _logger.exception('Neto sync: unhandled error on order %s', order_id)
            SyncLog.create({**base_vals, 'state': 'error', 'error_message': str(exc)})

    # -------------------------------------------------------------------------
    # Per-store sync
    # -------------------------------------------------------------------------

    def _sync_store(self, store, hours_back=None):
        if not store.api_key or not store.store_url:
            _logger.warning(
                'Neto connector: store "%s" missing api_key or store_url — skipping.',
                store.name,
            )
            return

        if hours_back is not None:
            since_dt = datetime.now(timezone.utc) - timedelta(hours=hours_back)
        elif store.last_sync_date:
            since_dt = store.last_sync_date.replace(tzinfo=timezone.utc)
        else:
            since_dt = datetime.now(timezone.utc) - timedelta(hours=24)

        _logger.info(
            'Neto sync [%s]: fetching orders updated since %s', store.name, since_dt
        )

        try:
            orders = self._fetch_orders(store, since_dt)
        except Exception as exc:
            _logger.error(
                'Neto sync [%s]: _fetch_orders raised unexpectedly — %s', store.name, exc
            )
            return

        store.sudo().write({'last_sync_date': fields.Datetime.now()})

        # Plain local variables — AbstractModel blocks instance attribute assignment
        synced_ids = set()
        debug_count = [0]  # [orders_logged_so_far]; mutable so _process_order can increment it

        _logger.info('Neto sync [%s]: %d order(s) to process', store.name, len(orders))
        for order_data in orders:
            self._process_order(order_data, store, synced_ids, debug_count)
            self.env.cr.commit()

        _logger.info('Neto sync [%s]: completed.', store.name)

    # -------------------------------------------------------------------------
    # Public entry point
    # -------------------------------------------------------------------------

    def run_sync(self, hours_back=None):
        stores = self.env['neto.store'].sudo().search([('active', '=', True)])
        _logger.info('Neto connector: run_sync called — %d active store(s) found', len(stores))
        if not stores:
            _logger.warning(
                'Neto connector: no active stores configured — aborting sync.'
            )
            return
        for store in stores:
            try:
                self._sync_store(store, hours_back=hours_back)
            except Exception as exc:
                _logger.exception(
                    'Neto connector: _sync_store failed for store "%s" — %s',
                    store.name, exc,
                )
