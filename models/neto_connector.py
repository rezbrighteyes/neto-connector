# -*- coding: utf-8 -*-
import logging
import requests
from datetime import datetime, timedelta, timezone

from markupsafe import Markup
from odoo import models, fields

_logger = logging.getLogger(__name__)

ALLOWED_STATUSES = frozenset({'New', 'Pick', 'Pack', 'Dispatched', 'Pending', 'New Backorder'})
_API_ACTION = 'GetOrder'
_GST_DIVISOR = 1.1  # Neto UnitPrice is GST-inclusive; divide to get ex-GST for Odoo
_SURCHARGE_SKU = 'NETO-SURCHARGE'  # internal product SKU for surcharge lines
_DISCOUNT_SKU  = 'NETO-DISCOUNT'   # internal product SKU for discount lines


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

    def _get_surcharge_product(self):
        """Return (or create) the surcharge service product."""
        Product = self.env['product.product'].sudo()
        product = Product.search([('default_code', '=', _SURCHARGE_SKU)], limit=1)
        if not product:
            product = Product.create({
                'name': 'Neto Order Surcharge',
                'default_code': _SURCHARGE_SKU,
                'type': 'service',
                'sale_ok': True,
                'purchase_ok': False,
                'invoice_policy': 'order',
            })
            _logger.info('Neto sync: created surcharge product (SKU=%s)', _SURCHARGE_SKU)
        return product

    def _get_discount_product(self):
        """Return (or create) the discount service product."""
        Product = self.env['product.product'].sudo()
        product = Product.search([('default_code', '=', _DISCOUNT_SKU)], limit=1)
        if not product:
            product = Product.create({
                'name': 'Neto Order Discount',
                'default_code': _DISCOUNT_SKU,
                'type': 'service',
                'sale_ok': True,
                'purchase_ok': False,
                'invoice_policy': 'order',
            })
            _logger.info('Neto sync: created discount product (SKU=%s)', _DISCOUNT_SKU)
        return product

    # -------------------------------------------------------------------------
    # API
    # -------------------------------------------------------------------------

    def _fetch_orders(self, store, since_dt, until_dt=None):
        url = f"{store.store_url.rstrip('/')}/do/WS/NetoAPI"
        headers = {
            'Content-Type': 'application/json',
            'NETOAPI_ACTION': _API_ACTION,
            'NETOAPI_KEY': store.api_key,
            'Accept': 'application/json',
        }
        date_filter = {
            'DateUpdatedFrom': since_dt.strftime('%Y-%m-%dT%H:%M:%S'),
        }
        if until_dt:
            date_filter['DateUpdatedTo'] = until_dt.strftime('%Y-%m-%dT%H:%M:%S')

        payload = {
            'Filter': {
                **date_filter,
                'OutputSelector': [
                    'OrderID', 'Username', 'GrandTotal', 'SurchargeTotal',
                    'DiscountTotal',
                    'OrderStatus', 'OrderLine', 'OrderLine.SKU',
                    'OrderLine.ProductName', 'OrderLine.UnitPrice',
                    'OrderLine.Quantity', 'BillingEmail', 'BillingName',
                    'BillingAddress', 'DatePlaced', 'DateUpdated',
                ],
            }
        }
        _logger.info(
            'Neto sync [%s]: POST %s  DateUpdatedFrom=%s%s',
            store.name, url, date_filter['DateUpdatedFrom'],
            f"  DateUpdatedTo={date_filter['DateUpdatedTo']}" if until_dt else '',
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
        order_status = order_data.get('OrderStatus', '') or ''

        order = Order.create({
            'partner_id': partner.id,
            'neto_order_id': order_data.get('OrderID'),
            'neto_order_status': order_status,
            'date_order': date_order,
            'warehouse_id': store.warehouse_id.id,
            'company_id': store.company_id.id,
        })

        raw_lines = order_data.get('OrderLine', [])
        if isinstance(raw_lines, dict):
            raw_lines = [raw_lines]

        line_prices = {}
        missing_lines = []  # collect unmatched SKUs for chatter

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
                missing_lines.append({
                    'sku': sku,
                    'name': line.get('ProductName') or '',
                    'qty': line.get('Quantity') or '',
                    'price': f"{float(line.get('UnitPrice') or 0):.2f}",
                })
                continue

            # Neto UnitPrice is GST-inclusive — strip GST before saving to Odoo
            neto_price_incl = float(line.get('UnitPrice') or 0)
            neto_price_excl = round(neto_price_incl / _GST_DIVISOR, 4)

            try:
                OrderLine.create({
                    'order_id': order.id,
                    'product_id': product.id,
                    'product_uom_qty': float(line.get('Quantity') or 1),
                    'price_unit': neto_price_excl,
                    'name': product.name,
                    'product_uom_id': product.uom_id.id,
                })
                line_prices[product.id] = neto_price_excl
            except Exception as line_exc:
                _logger.warning(
                    'Neto sync: could not create line SKU=%s on order %s — %s',
                    sku, order_data.get('OrderID'), line_exc,
                )

        # Surcharge line
        surcharge_total = float(order_data.get('SurchargeTotal') or 0)
        if surcharge_total > 0:
            surcharge_product = self._get_surcharge_product()
            surcharge_excl = round(surcharge_total / _GST_DIVISOR, 4)
            try:
                OrderLine.create({
                    'order_id': order.id,
                    'product_id': surcharge_product.id,
                    'product_uom_qty': 1,
                    'price_unit': surcharge_excl,
                    'name': 'Neto Order Surcharge',
                    'product_uom_id': surcharge_product.uom_id.id,
                })
                _logger.info(
                    'Neto sync: added surcharge line $%.4f (ex-GST) on order %s',
                    surcharge_excl, order_data.get('OrderID'),
                )
            except Exception as sc_exc:
                _logger.warning(
                    'Neto sync: could not create surcharge line on order %s — %s',
                    order_data.get('OrderID'), sc_exc,
                )

        # Discount line (negative service line, GST-inclusive amount from Neto)
        discount_total = float(order_data.get('DiscountTotal') or 0)
        if discount_total > 0:
            discount_product = self._get_discount_product()
            discount_excl = round(discount_total / _GST_DIVISOR, 4)
            try:
                OrderLine.create({
                    'order_id': order.id,
                    'product_id': discount_product.id,
                    'product_uom_qty': 1,
                    'price_unit': -discount_excl,  # negative to reduce order total
                    'name': 'Neto Order Discount',
                    'product_uom_id': discount_product.uom_id.id,
                })
                _logger.info(
                    'Neto sync: added discount line -$%.4f (ex-GST) on order %s',
                    discount_excl, order_data.get('OrderID'),
                )
            except Exception as dc_exc:
                _logger.warning(
                    'Neto sync: could not create discount line on order %s — %s',
                    order_data.get('OrderID'), dc_exc,
                )

        # Confirm order — wrapped safely; staging env may lack stock.move.group_id
        try:
            order.action_confirm()
            for ol in order.order_line:
                neto_price = line_prices.get(ol.product_id.id)
                if neto_price is not None and ol.price_unit != neto_price:
                    ol.sudo().write({'price_unit': neto_price})
            if date_order:
                order.sudo().write({'date_order': date_order})
        except AttributeError as ae:
            # Staging env missing stock.move.group_id — leave as draft, log once
            _logger.info(
                'Neto sync: order %s left as draft (action_confirm skipped: %s)',
                order_data.get('OrderID'), ae,
            )
            if date_order:
                order.sudo().write({'date_order': date_order})
        except Exception as confirm_exc:
            _logger.warning(
                'Neto sync: order %s created as draft — action_confirm() failed: %s',
                order_data.get('OrderID'), confirm_exc,
            )

        # Post missing SKU chatter message as proper HTML (Markup prevents auto-escaping)
        if missing_lines:
            rows = Markup('').join(
                Markup(
                    '<tr style="border-bottom:1px solid #e0e0e0;">'
                    '<td style="padding:4px 10px;font-family:monospace;">{sku}</td>'
                    '<td style="padding:4px 10px;">{name}</td>'
                    '<td style="padding:4px 10px;text-align:center;">{qty}</td>'
                    '<td style="padding:4px 10px;text-align:right;">${price} '
                    '<small style="color:#888;">(GST-inc)</small></td>'
                    '</tr>'
                ).format(
                    sku=m['sku'],
                    name=m['name'],
                    qty=m['qty'],
                    price=m['price'],
                )
                for m in missing_lines
            )
            msg = Markup(
                '<p>\u26a0\ufe0f <strong>The following Neto lines could not be matched to an '
                'Odoo product and were <u>NOT</u> added to this order:</strong></p>'
                '<table style="border-collapse:collapse;width:100%;font-size:13px;">'
                '<thead><tr style="background:#f5f5f5;font-weight:600;">'
                '<th style="padding:5px 10px;text-align:left;">SKU</th>'
                '<th style="padding:5px 10px;">Product Name</th>'
                '<th style="padding:5px 10px;text-align:center;">Qty</th>'
                '<th style="padding:5px 10px;text-align:right;">Unit Price</th>'
                '</tr></thead>'
                '<tbody>{rows}</tbody></table>'
            ).format(rows=rows)
            order.sudo().message_post(body=msg)

        return order, missing_lines

    # -------------------------------------------------------------------------
    # Per-order processing
    # -------------------------------------------------------------------------

    def _process_order(self, order_data, store, synced_ids):
        """Process a single Neto order dict."""
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
            'neto_order_status': order_status,
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
            sale_order, missing_lines = self._create_sale_order(order_data, partner, store)
            line_count = len(sale_order.order_line)

            missing_skus_text = ', '.join(m['sku'] for m in missing_lines) if missing_lines else False

            if line_count == 0:
                SyncLog.create({
                    **base_vals,
                    'state': 'skipped',
                    'sale_order_id': sale_order.id,
                    'partner_id': partner.id,
                    'partner_created': partner_created,
                    'line_count': 0,
                    'missing_skus': missing_skus_text,
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
                'missing_skus': missing_skus_text,
            })

        except Exception as exc:
            _logger.exception('Neto sync: unhandled error on order %s', order_id)
            SyncLog.create({**base_vals, 'state': 'error', 'error_message': str(exc)})

    # -------------------------------------------------------------------------
    # Per-store sync
    # -------------------------------------------------------------------------

    def _sync_store(self, store, hours_back=None, since_dt=None, until_dt=None):
        if not store.api_key or not store.store_url:
            _logger.warning(
                'Neto connector: store "%s" missing api_key or store_url — skipping.',
                store.name,
            )
            return

        if since_dt:
            pass
        elif hours_back is not None:
            since_dt = datetime.now(timezone.utc) - timedelta(hours=hours_back)
        elif store.last_sync_date:
            since_dt = store.last_sync_date.replace(tzinfo=timezone.utc)
        else:
            since_dt = datetime.now(timezone.utc) - timedelta(hours=24)

        _logger.info(
            'Neto sync [%s]: fetching orders updated since %s%s',
            store.name, since_dt,
            f' until {until_dt}' if until_dt else '',
        )

        try:
            orders = self._fetch_orders(store, since_dt, until_dt=until_dt)
        except Exception as exc:
            _logger.error(
                'Neto sync [%s]: _fetch_orders raised unexpectedly — %s', store.name, exc
            )
            return

        store.sudo().write({'last_sync_date': fields.Datetime.now()})

        synced_ids = set()
        _logger.info('Neto sync [%s]: %d order(s) to process', store.name, len(orders))
        for order_data in orders:
            self._process_order(order_data, store, synced_ids)
            self.env.cr.commit()

        _logger.info('Neto sync [%s]: completed.', store.name)

    # -------------------------------------------------------------------------
    # Public entry point (cron)
    # -------------------------------------------------------------------------

    def run_sync(self, hours_back=None):
        stores = self.env['neto.store'].sudo().search([('active', '=', True)])
        _logger.info('Neto connector: run_sync called — %d active store(s) found', len(stores))
        if not stores:
            _logger.warning('Neto connector: no active stores configured — aborting sync.')
            return
        for store in stores:
            try:
                self._sync_store(store, hours_back=hours_back)
            except Exception as exc:
                _logger.exception(
                    'Neto connector: _sync_store failed for store "%s" — %s',
                    store.name, exc,
                )
