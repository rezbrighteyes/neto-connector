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
_SHIPPING_SKU  = 'NETO_SHIPPING'   # internal product SKU for shipping lines


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

    def _get_shipping_product(self):
        """Return the NETO_SHIPPING service product (must already exist in Odoo)."""
        product = self.env['product.product'].sudo().search(
            [('default_code', '=', _SHIPPING_SKU)], limit=1
        )
        if not product:
            _logger.warning(
                'Neto sync: shipping product SKU=%s not found — shipping line skipped',
                _SHIPPING_SKU,
            )
        return product

    def _get_or_create_ship_address(self, partner, order_data):
        """Return a child delivery address partner for this order's ShipAddress fields.

        Neto returns shipping address as flat top-level keys when 'ShipAddress'
        OutputSelector is requested:
            ShipFirstName, ShipLastName, ShipCompany,
            ShipStreetLine1, ShipStreetLine2,
            ShipCity, ShipState, ShipPostCode, ShipCountry, ShipPhone
        There is no nested ShipAddress dict.

        We match on street + postcode to avoid creating duplicates on re-sync.
        """
        Partner = self.env['res.partner'].sudo()

        first   = (order_data.get('ShipFirstName') or '').strip()
        last    = (order_data.get('ShipLastName')  or '').strip()
        company = (order_data.get('ShipCompany')   or '').strip()
        street1 = (order_data.get('ShipStreetLine1') or '').strip()
        street2 = (order_data.get('ShipStreetLine2') or '').strip()
        city    = (order_data.get('ShipCity')      or '').strip()
        state   = (order_data.get('ShipState')     or '').strip()
        postcode= (order_data.get('ShipPostCode')  or '').strip()
        country_raw = (order_data.get('ShipCountry') or 'AU').strip()
        phone   = (order_data.get('ShipPhone')     or '').strip()

        # Display name: prefer company, then full name, then parent name
        if company:
            display_name = company
        elif first or last:
            display_name = f"{first} {last}".strip()
        else:
            display_name = partner.name

        # Resolve country
        country = self.env['res.country'].sudo().search(
            ['|', ('code', '=ilike', country_raw),
                  ('name', '=ilike', country_raw)], limit=1
        )
        country_id = country.id if country else False

        # Resolve state
        state_id = False
        if country and state:
            state_rec = self.env['res.country.state'].sudo().search(
                [('country_id', '=', country.id),
                 '|', ('code', '=ilike', state),
                      ('name', '=ilike', state)], limit=1
            )
            state_id = state_rec.id if state_rec else False

        # Try to find an existing matching delivery address child of this partner
        existing = Partner.search([
            ('parent_id', '=', partner.id),
            ('type', '=', 'delivery'),
            ('street', '=', street1 or False),
            ('zip',    '=', postcode or False),
        ], limit=1)
        if existing:
            return existing

        vals = {
            'parent_id':  partner.id,
            'type':       'delivery',
            'name':       display_name,
            'street':     street1 or False,
            'street2':    street2 or False,
            'city':       city or False,
            'zip':        postcode or False,
            'phone':      phone or False,
            'country_id': country_id,
            'state_id':   state_id,
        }
        ship_partner = Partner.create(vals)
        _logger.info(
            'Neto sync: created delivery address for partner %s (ship to: %s)',
            partner.name, display_name,
        )
        return ship_partner

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
                # Neto GetOrder OutputSelector reference:
                # https://developers.maropost.com/documentation/engineers/api-documentation/orders-invoices/getorder
                #
                # BillAddress / ShipAddress are top-level selectors — Neto returns
                # billing/shipping fields flat on the order object (e.g. BillFirstName,
                # ShipStreetLine1, etc.).  There are NO sub-selectors like
                # BillAddress.BillCity or ShipAddress.ShipCity.
                #
                # OrderPayment is an array of payment records; we use the first entry's
                # PaymentType.  It may arrive as a dict (single payment) — always
                # normalise to list.
                'OutputSelector': [
                    'OrderID', 'Username', 'Email',
                    'BillAddress',
                    'ShipAddress',
                    'GrandTotal', 'SurchargeTotal', 'ShippingTotal',
                    'OrderStatus',
                    'OrderLine', 'OrderLine.SKU',
                    'OrderLine.ProductName', 'OrderLine.UnitPrice',
                    'OrderLine.Quantity', 'OrderLine.PercentDiscount',
                    'OrderLine.ProductDiscount',
                    'DatePlaced', 'DateUpdated',
                    'DatePaid',
                    'OrderPayment',
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
                'Neto sync [%s]: API request failed \u2014 %s', store.name, exc
            )
            return []

        try:
            body = response.json()
        except Exception as exc:
            _logger.error(
                'Neto sync [%s]: could not parse JSON response \u2014 %s\nRaw: %s',
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

    def _get_or_create_partner(self, username, order_data):
        """Find partner by neto_username or create one from the order's billing fields.

        Neto returns billing data as flat top-level keys on the order object when
        the 'BillAddress' and 'Email' OutputSelectors are requested:
            BillFirstName, BillLastName, BillCompany,
            BillStreetLine1, BillStreetLine2,
            BillCity, BillState, BillPostCode, BillCountry, BillPhone
        There is no nested BillingAddress dict.
        """
        Partner = self.env['res.partner'].sudo()
        partner = Partner.search([('neto_username', '=', username)], limit=1)
        if partner:
            return partner, False

        # Build display name: prefer company, then first+last, then username
        first = (order_data.get('BillFirstName') or '').strip()
        last  = (order_data.get('BillLastName') or '').strip()
        company = (order_data.get('BillCompany') or '').strip()
        if company:
            display_name = company
        elif first or last:
            display_name = f"{first} {last}".strip()
        else:
            display_name = username

        email = (order_data.get('Email') or '').strip()

        vals = {
            'neto_username': username,
            'ref': username,
            'name': display_name,
            'email': email or False,
            'is_company': bool(company),
            'customer_rank': 1,
            'street':  order_data.get('BillStreetLine1') or False,
            'street2': order_data.get('BillStreetLine2') or False,
            'city':    order_data.get('BillCity') or False,
            'zip':     order_data.get('BillPostCode') or False,
            'phone':   order_data.get('BillPhone') or False,
        }

        country_code = (order_data.get('BillCountry') or '').strip()
        if country_code:
            country = self.env['res.country'].sudo().search(
                ['|', ('code', '=ilike', country_code),
                      ('name', '=ilike', country_code)], limit=1
            )
            if country:
                vals['country_id'] = country.id
                state_name = (order_data.get('BillState') or '').strip()
                if state_name:
                    state = self.env['res.country.state'].sudo().search(
                        [('country_id', '=', country.id),
                         '|', ('code', '=ilike', state_name),
                              ('name', '=ilike', state_name)], limit=1
                    )
                    if state:
                        vals['state_id'] = state.id

        partner = Partner.create(vals)
        _logger.info(
            'Neto sync: created partner "%s" (username=%s, email=%s)',
            display_name, username, email,
        )
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

        # --- DatePaid ---
        date_paid = self._parse_neto_datetime(order_data.get('DatePaid'))

        # --- Payment method: normalise OrderPayment to list, use first entry ---
        payments = order_data.get('OrderPayment', []) or []
        if isinstance(payments, dict):
            payments = [payments]
        payment_method = (payments[0].get('PaymentType') or '').strip() if payments else ''

        # --- Shipping delivery address child partner ---
        ship_partner = self._get_or_create_ship_address(partner, order_data)

        order_status = order_data.get('OrderStatus', '') or ''

        order_vals = {
            'partner_id':           partner.id,
            'partner_shipping_id':  ship_partner.id,
            'neto_order_id':        order_data.get('OrderID'),
            'neto_order_status':    order_status,
            'date_order':           date_order,
            'warehouse_id':         store.warehouse_id.id,
            'company_id':           store.company_id.id,
        }
        # Write DatePaid / PaymentMethod only if the fields exist on sale.order
        # (they are added by this module's sale_order.py extension)
        if date_paid and 'neto_date_paid' in self.env['sale.order']._fields:
            order_vals['neto_date_paid'] = date_paid
        if payment_method and 'neto_payment_method' in self.env['sale.order']._fields:
            order_vals['neto_payment_method'] = payment_method

        order = Order.create(order_vals)

        raw_lines = order_data.get('OrderLine', [])
        if isinstance(raw_lines, dict):
            raw_lines = [raw_lines]

        line_prices = {}   # product_id -> (price_excl, discount_pct)
        missing_lines = []  # collect unmatched SKUs for chatter

        for line in raw_lines:
            sku = (line.get('SKU') or line.get('Sku') or '').strip()
            if not sku:
                _logger.warning(
                    'Neto sync: order %s has a line with no SKU \u2014 skipping line',
                    order_data.get('OrderID'),
                )
                continue
            product = Product.search([('default_code', '=', sku)], limit=1)
            if not product:
                _logger.warning(
                    'Neto sync: SKU "%s" not found on order %s \u2014 skipping line',
                    sku, order_data.get('OrderID'),
                )
                missing_lines.append({
                    'sku': sku,
                    'name': line.get('ProductName') or '',
                    'qty': line.get('Quantity') or '',
                    'price': f"{float(line.get('UnitPrice') or 0):.2f}",
                })
                continue

            # Neto UnitPrice is GST-inclusive \u2014 strip GST before saving to Odoo
            neto_price_incl = float(line.get('UnitPrice') or 0)
            neto_price_excl = round(neto_price_incl / _GST_DIVISOR, 4)

            # Line-level discount: prefer PercentDiscount; fall back to
            # ProductDiscount (dollar amount) converted to a percentage.
            percent_discount = float(line.get('PercentDiscount') or 0)
            if not percent_discount:
                product_discount_amt = float(line.get('ProductDiscount') or 0)
                if product_discount_amt and neto_price_incl:
                    percent_discount = round(
                        product_discount_amt / neto_price_incl * 100, 4
                    )

            try:
                OrderLine.create({
                    'order_id': order.id,
                    'product_id': product.id,
                    'product_uom_qty': float(line.get('Quantity') or 1),
                    'price_unit': neto_price_excl,
                    'discount': percent_discount,
                    'name': product.name,
                    'product_uom_id': product.uom_id.id,
                })
                line_prices[product.id] = (neto_price_excl, percent_discount)
            except Exception as line_exc:
                _logger.warning(
                    'Neto sync: could not create line SKU=%s on order %s \u2014 %s',
                    sku, order_data.get('OrderID'), line_exc,
                )

        # --- Surcharge line ---
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
                    'Neto sync: could not create surcharge line on order %s \u2014 %s',
                    order_data.get('OrderID'), sc_exc,
                )

        # --- Shipping line ---
        # ShippingTotal is already ex-GST on Neto; NETO_SHIPPING product carries 10% GST
        shipping_total = float(order_data.get('ShippingTotal') or 0)
        if shipping_total > 0:
            shipping_product = self._get_shipping_product()
            if shipping_product:
                shipping_excl = round(shipping_total / _GST_DIVISOR, 4)
                try:
                    OrderLine.create({
                        'order_id': order.id,
                        'product_id': shipping_product.id,
                        'product_uom_qty': 1,
                        'price_unit': shipping_excl,
                        'name': order_data.get('ShippingOption') or 'Shipping',
                        'product_uom_id': shipping_product.uom_id.id,
                    })
                    _logger.info(
                        'Neto sync: added shipping line $%.4f (ex-GST) on order %s',
                        shipping_excl, order_data.get('OrderID'),
                    )
                except Exception as sh_exc:
                    _logger.warning(
                        'Neto sync: could not create shipping line on order %s \u2014 %s',
                        order_data.get('OrderID'), sh_exc,
                    )

        # Confirm order \u2014 wrapped safely; staging env may lack stock.move.group_id
        try:
            order.action_confirm()
            for ol in order.order_line:
                neto = line_prices.get(ol.product_id.id)
                if neto is not None:
                    price, disc = neto
                    writes = {}
                    if ol.price_unit != price:
                        writes['price_unit'] = price
                    if ol.discount != disc:
                        writes['discount'] = disc
                    if writes:
                        ol.sudo().write(writes)
            if date_order:
                order.sudo().write({'date_order': date_order})
        except AttributeError as ae:
            _logger.info(
                'Neto sync: order %s left as draft (action_confirm skipped: %s)',
                order_data.get('OrderID'), ae,
            )
            if date_order:
                order.sudo().write({'date_order': date_order})
        except Exception as confirm_exc:
            _logger.warning(
                'Neto sync: order %s created as draft \u2014 action_confirm() failed: %s',
                order_data.get('OrderID'), confirm_exc,
            )

        # Post missing SKU chatter message as proper HTML
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
        billing_email = (order_data.get('Email') or '').strip()
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
            # Rule 3: duplicate \u2014 silent, no log entry
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

            partner, partner_created = self._get_or_create_partner(username, order_data)
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
                'Neto connector: store "%s" missing api_key or store_url \u2014 skipping.',
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
                'Neto sync [%s]: _fetch_orders raised unexpectedly \u2014 %s', store.name, exc
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
        _logger.info('Neto connector: run_sync called \u2014 %d active store(s) found', len(stores))
        if not stores:
            _logger.warning('Neto connector: no active stores configured \u2014 aborting sync.')
            return
        for store in stores:
            try:
                self._sync_store(store, hours_back=hours_back)
            except Exception as exc:
                _logger.exception(
                    'Neto connector: _sync_store failed for store "%s" \u2014 %s',
                    store.name, exc,
                )
