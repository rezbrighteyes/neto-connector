# -*- coding: utf-8 -*-
import logging
import requests
from datetime import datetime, timedelta, timezone

from markupsafe import Markup
from odoo import models, fields

_logger = logging.getLogger(__name__)

_API_ACTION = 'GetOrder'
_PAYMENT_API_ACTION = 'GetPayment'
_GST_DIVISOR = 1.1  # Neto UnitPrice is GST-inclusive; divide to get ex-GST for Odoo
_SURCHARGE_SKU = 'NETO-SURCHARGE'  # internal product SKU for surcharge lines
_SHIPPING_SKU  = 'NETO_SHIPPING'   # internal product SKU for shipping lines

# Neto internal line-type prefixes / exact SKUs.
#   TEXT_NOTE  — free-text note lines: collected and shown as FYI in chatter
#   DS_*       — drop-ship instruction lines: silently dropped
_SKIP_SKU_PREFIXES = (
    'DS_',
    'REPVISIT_',
    'p_header_',
    'stand_',
    'catalogue_',
    'header_',
)
_NOTE_SKU_EXACT = frozenset({'TEXT_NOTE'})

# Suffix appended to auto-created product names so they are easy to spot
_NETO_UNSYNCED_SUFFIX = '[NETO-UNSYNCED]'

# Internal email domain — orders from this domain are synced but flagged
_INTERNAL_EMAIL_DOMAIN = '@brighteyes.net.au'

# Neto statuses that should cancel the Odoo order
_CANCEL_STATUSES = frozenset({'Cancelled', 'Declined'})

# Neto statuses that are dispatched (confirmed + flagged via neto_order_status)
_DISPATCHED_STATUSES = frozenset({'Dispatched'})

# Only create invoices for orders placed within this many days.
# Exception: orders with full payment recorded in Neto are always invoiced.
_INVOICE_CUTOFF_DAYS = 730

# GetOrder page size — keep pages small so history jobs checkpoint often.
_GET_ORDER_PAGE_SIZE = 25

_GET_PAYMENT_PAGE_SIZE = 100

GET_PAYMENT_OUTPUT_SELECTOR = [
    'PaymentID',
    'OrderID',
    'AmountPaid',
    'CurrencyCode',
    'DatePaidUTC',
    'PaymentMethod',
    'PaymentMethodName',
    'ProcessBy',
    'PaymentNotes',
]

# GetItem OutputSelectors we need for product creation
_GETITEM_OUTPUT = [
    'ID', 'InventoryID',
    'SKU', 'ParentSKU',
    'Name', 'Brand', 'Model',
    'DefaultPrice', 'RRP', 'CostPrice', 'DefaultPurchasePrice',
    'TaxInclusive',
    'UPC', 'UPC1',
    'ShippingWeight',
    'IsActive', 'IsVariant',
    'WarehouseQuantity',
    'AvailableSellQuantity',
    'CommittedQuantity',
    'Categories', 'ReferenceNumber', 'PriceGroups',
]

# GetOrder OutputSelector — single source of truth used by both connector and wizard
GET_ORDER_OUTPUT_SELECTOR = [
    'OrderID', 'Username', 'Email',
    'BillAddress',
    'ShipAddress',
    'GrandTotal', 'SurchargeTotal', 'ShippingTotal', 'ShippingOption',
    'OrderStatus',
    'PaymentMethod',
    'OrderPayment',          # replaces GetPayment API call
    'OrderLine', 'OrderLine.SKU',
    'OrderLine.ProductName', 'OrderLine.UnitPrice',
    'OrderLine.Quantity', 'OrderLine.PercentDiscount',
    'OrderLine.ProductDiscount',
    'OrderLine.ShippingTracking',
    'DatePlaced', 'DateUpdated',
    'DatePaid',
]


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

    def _parse_neto_date(self, raw):
        """Return a date string (YYYY-MM-DD) from a Neto datetime string, or False.

        Slices the date portion directly from the raw string to avoid UTC
        conversion rolling the date back for AEST (UTC+10/11) timestamps.
        """
        if not raw:
            return False
        return str(raw)[:10]

    def _is_internal_sku(self, sku):
        """Return True for SKUs that should be silently skipped."""
        return any(sku.startswith(pfx) for pfx in _SKIP_SKU_PREFIXES)

    def _is_note_sku(self, sku):
        """Return True for TEXT_NOTE lines that should appear as FYI notes in chatter."""
        return sku in _NOTE_SKU_EXACT

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
        """Return (or create) the shipping service product."""
        product = self.env['product.product'].sudo().search(
            [('default_code', '=', _SHIPPING_SKU)], limit=1
        )
        if not product:
            product = self.env['product.product'].sudo().create({
                'name': 'Neto Shipping',
                'default_code': _SHIPPING_SKU,
                'type': 'service',
                'sale_ok': True,
                'purchase_ok': False,
                'invoice_policy': 'order',
            })
            _logger.info('Neto sync: created shipping product (SKU=%s)', _SHIPPING_SKU)
        return product

    def _safe_float(self, value):
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0

    def _add_neto_total_line(self, order, product, amount_incl, name, existing_skus=None):
        sku = (product.default_code or '').strip()
        if amount_incl <= 0:
            return False
        if existing_skus is not None and sku in existing_skus:
            return False

        amount_excl = round(amount_incl / _GST_DIVISOR, 4)
        try:
            line = self.env['sale.order.line'].sudo().create({
                'order_id':       order.id,
                'product_id':     product.id,
                'product_uom_qty': 1,
                'price_unit':     amount_excl,
                'name':           name,
                'product_uom_id': product.uom_id.id,
            })
        except Exception as exc:
            _logger.warning(
                'Neto sync: could not create %s line on order %s — %s',
                sku, order.neto_order_id or order.name, exc,
            )
            return False

        if existing_skus is not None and sku:
            existing_skus.add(sku)
        _logger.info(
            'Neto sync: added %s line $%.4f (ex-GST) on order %s',
            sku, amount_excl, order.neto_order_id or order.name,
        )
        return line

    def _add_neto_total_lines(self, order, order_data, existing_skus=None):
        """Add Neto order-level surcharge and shipping lines, avoiding duplicates."""
        added = []

        surcharge_total = self._safe_float(order_data.get('SurchargeTotal'))
        if surcharge_total > 0:
            surcharge_product = self._get_surcharge_product()
            surcharge_line = self._add_neto_total_line(
                order,
                surcharge_product,
                surcharge_total,
                'Neto Order Surcharge',
                existing_skus=existing_skus,
            )
            if surcharge_line:
                added.append(surcharge_line)

        shipping_total = self._safe_float(order_data.get('ShippingTotal'))
        if shipping_total > 0:
            shipping_product = self._get_shipping_product()
            shipping_line = self._add_neto_total_line(
                order,
                shipping_product,
                shipping_total,
                order_data.get('ShippingOption') or 'Shipping',
                existing_skus=existing_skus,
            )
            if shipping_line:
                added.append(shipping_line)

        return added

    def _get_consignment_number_from_order(self, order_data):
        raw_lines = order_data.get('OrderLine', [])
        if isinstance(raw_lines, dict):
            raw_lines = [raw_lines]
        tracking_numbers = []
        seen = set()
        for line in raw_lines or []:
            tracking = (line.get('ShippingTracking') or '').strip()
            if tracking and tracking not in seen:
                tracking_numbers.append(tracking)
                seen.add(tracking)
        return ', '.join(tracking_numbers)

    # -------------------------------------------------------------------------
    # Payment helpers — read from OrderPayment block in GetOrder response
    # -------------------------------------------------------------------------

    def _get_payment_info_from_order(self, order_data, order_id):
        """Extract payment info directly from the GetOrder OrderPayment block.

        Returns (payment_method, amount_paid, date_paid, is_partial).
        No API call — data is already in the GetOrder response.
        """
        payments = order_data.get('OrderPayment', [])
        if isinstance(payments, dict):
            payments = [payments]
        payments = payments or []

        if not payments:
            # Fall back to order-level PaymentMethod field
            method = (order_data.get('PaymentMethod') or '').strip() or None
            _logger.debug(
                'Neto sync: order %s — no OrderPayment block, '
                'order-level PaymentMethod=%r', order_id, method,
            )
            return method, 0.0, None, False

        # Sum all payment amounts with rounding to avoid float drift
        total_paid = round(sum(float(p.get('Amount', 0)) for p in payments), 2)

        # Most recent payment date
        dates = [p.get('DatePaid') for p in payments if p.get('DatePaid')]
        raw_date = max(dates) if dates else None
        date_paid = self._parse_neto_datetime(raw_date)

        # PaymentMethod not in OrderPayment block — use order-level field
        method = (order_data.get('PaymentMethod') or '').strip() or None

        grand_total = round(float(order_data.get('GrandTotal', 0)), 2)
        is_partial = (grand_total > 0) and (total_paid < grand_total - 0.01)

        _logger.debug(
            'Neto sync: order %s — OrderPayment: total_paid=%.2f, '
            'grand_total=%.2f, method=%r, is_partial=%s',
            order_id, total_paid, grand_total, method, is_partial,
        )
        return method, total_paid, date_paid, is_partial

    def _is_fully_paid(self, order_data):
        """Return True if OrderPayment total >= GrandTotal (within 1 cent tolerance)."""
        payments = order_data.get('OrderPayment', [])
        if isinstance(payments, dict):
            payments = [payments]
        if not payments:
            return False
        total_paid = round(sum(float(p.get('Amount', 0)) for p in payments), 2)
        grand_total = round(float(order_data.get('GrandTotal', 0)), 2)
        return grand_total > 0 and total_paid >= grand_total - 0.01

    def _get_or_create_ship_address(self, partner, order_data):
        """Return a child delivery address partner for this order's ShipAddress fields."""
        Partner = self.env['res.partner'].sudo()

        first    = (order_data.get('ShipFirstName') or '').strip()
        last     = (order_data.get('ShipLastName')  or '').strip()
        company  = (order_data.get('ShipCompany')   or '').strip()
        street1  = (order_data.get('ShipStreetLine1') or '').strip()
        street2  = (order_data.get('ShipStreetLine2') or '').strip()
        city     = (order_data.get('ShipCity')      or '').strip()
        state    = (order_data.get('ShipState')     or '').strip()
        postcode = (order_data.get('ShipPostCode')  or '').strip()
        country_raw = (order_data.get('ShipCountry') or 'AU').strip()
        phone    = (order_data.get('ShipPhone')     or '').strip()

        if company:
            display_name = company
        elif first or last:
            display_name = f"{first} {last}".strip()
        else:
            display_name = partner.name

        country = self.env['res.country'].sudo().search(
            ['|', ('code', '=ilike', country_raw),
                  ('name', '=ilike', country_raw)], limit=1
        )
        country_id = country.id if country else False

        state_id = False
        if country and state:
            state_rec = self.env['res.country.state'].sudo().search(
                [('country_id', '=', country.id),
                 '|', ('code', '=ilike', state),
                      ('name', '=ilike', state)], limit=1
            )
            state_id = state_rec.id if state_rec else False

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
            'company_id': False,
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

    def _ensure_partner_company_compatibility(self, partner, store, username=None):
        """Make Neto-linked customer partners company-neutral when needed.

        Neto history and multi-store syncs can legitimately reuse the same
        partner across companies. A company-bound partner causes sale.order
        creation to fail when the order belongs to another company, so we
        relax the partner to shared/company-neutral in that case.
        """
        if not partner.company_id or partner.company_id == store.company_id:
            return partner

        _logger.warning(
            'Neto sync: partner "%s" (username=%s) belongs to company "%s" '
            'but store "%s" uses company "%s" — clearing partner company_id',
            partner.display_name,
            username or partner.neto_username or '',
            partner.company_id.display_name,
            store.name,
            store.company_id.display_name,
        )
        partner.sudo().write({'company_id': False})
        return partner

    # -------------------------------------------------------------------------
    # Auto-create missing products via GetItem
    # -------------------------------------------------------------------------

    def _fetch_neto_items_by_filter(self, store, filter_values, timeout=30):
        """Call Neto GetItem and return a normalized list of item dicts."""
        url = f"{store.store_url.rstrip('/')}/do/WS/NetoAPI"
        headers = {
            'Content-Type': 'application/json',
            'NETOAPI_ACTION': 'GetItem',
            'NETOAPI_KEY': store.api_key,
            'Accept': 'application/json',
        }
        payload = {
            'Filter': {
                'OutputSelector': _GETITEM_OUTPUT,
            }
        }
        payload['Filter'].update(filter_values)
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=timeout)
            response.raise_for_status()
            body = response.json()
        except Exception as exc:
            _logger.warning('Neto sync: GetItem failed for filter %s — %s', filter_values, exc)
            return []

        if hasattr(self, '_extract_items_from_getitem_response'):
            return self._extract_items_from_getitem_response(body)

        if 'GetItemResponse' in body:
            items = body['GetItemResponse'].get('Item', [])
        else:
            items = body.get('Item', [])
        if isinstance(items, dict):
            items = [items]
        return items or []

    def _fetch_neto_item(self, store, sku):
        """Call Neto GetItem for a single SKU. Returns the item dict or None."""
        items = self._fetch_neto_items_by_filter(store, {'SKU': [sku]})
        if not items:
            _logger.warning('Neto sync: GetItem returned no data for SKU %s', sku)
            return None
        return items[0]

    def _fetch_neto_variant_items(self, store, parent_sku):
        """Return Neto variant rows for a parent/generic SKU."""
        parent_sku = (parent_sku or '').strip()
        if not parent_sku:
            return []
        items = self._fetch_neto_items_by_filter(store, {'ParentSKU': [parent_sku]})
        if not items:
            _logger.warning('Neto sync: GetItem returned no variant data for ParentSKU %s', parent_sku)
        return items

    def _prepare_product_for_store_company(self, product, store):
        """Make a matched product usable on orders for the store company."""
        if not product or not store.company_id:
            return product

        product = product.sudo()
        template = product.product_tmpl_id.sudo()

        if 'company_id' in product._fields and product.company_id and product.company_id != store.company_id:
            product.write({'company_id': False})

        if 'company_id' in template._fields and template.company_id and template.company_id != store.company_id:
            template.write({'company_id': False})

        if 'company_ids' in template._fields and store.company_id.id not in template.company_ids.ids:
            template.write({'company_ids': [(4, store.company_id.id)]})

        return product.with_company(store.company_id)

    def _get_or_create_product_from_neto(self, store, sku, line_data):
        """Look up SKU in Odoo; if missing, call GetItem and create a placeholder product.

        Returns (product, was_created).
        """
        Product = self.env['product.product'].sudo()
        conflict_on_default_code = False
        products = Product.search([('default_code', '=', sku)])
        if hasattr(self, '_select_active_unique_match'):
            product, conflict_on_default_code = self._select_active_unique_match(products)
            if product:
                return self._prepare_product_for_store_company(product, store), False
        elif len(products) == 1:
            return self._prepare_product_for_store_company(products, store), False

        item = self._fetch_neto_item(store, sku)
        if item:
            if hasattr(self, '_match_existing_product'):
                product, conflict = self._match_existing_product(store, item)
                if product:
                    return self._prepare_product_for_store_company(product, store), False
                if conflict:
                    _logger.warning(
                        'Neto sync: SKU=%s remains ambiguous after Neto item lookup — '
                        'creating [NETO-UNSYNCED] placeholder to preserve the order line',
                        sku,
                    )
            neto_barcode = (item.get('UPC') or '').strip()
            if neto_barcode:
                product = Product.search([('barcode', '=', neto_barcode)], limit=1)
                if product:
                    _logger.info(
                        'Neto sync: SKU=%s matched existing product "%s" via barcode %s',
                        sku, product.name, neto_barcode,
                    )
                    return self._prepare_product_for_store_company(product, store), False
            neto_generic_barcode = (item.get('UPC1') or '').strip()
            if neto_generic_barcode and 'reza_generic_barcode' in Product._fields:
                product = Product.search([
                    ('reza_generic_barcode', '=', neto_generic_barcode),
                ], limit=2)
                if len(product) == 1:
                    _logger.info(
                        'Neto sync: SKU=%s matched existing product "%s" via generic barcode %s',
                        sku, product.name, neto_generic_barcode,
                    )
                    return self._prepare_product_for_store_company(product, store), False

        if conflict_on_default_code:
            _logger.warning(
                'Neto sync: SKU=%s matches multiple Odoo products by default_code and '
                'could not be resolved — creating [NETO-UNSYNCED] placeholder',
                sku,
            )

        if item and item.get('Name'):
            base_name = item['Name'].strip()
        elif line_data.get('ProductName'):
            base_name = line_data['ProductName'].strip()
        else:
            base_name = sku

        product_name = f"{base_name} {_NETO_UNSYNCED_SUFFIX}"

        list_price = 0.0
        if item:
            raw_price = float(item.get('DefaultPrice') or 0)
            tax_inclusive = str(item.get('TaxInclusive') or '').strip().lower()
            if tax_inclusive in ('true', '1', 'yes') and raw_price:
                list_price = round(raw_price / _GST_DIVISOR, 4)
            elif raw_price:
                list_price = round(raw_price, 4)

        if not list_price:
            line_price_incl = float(line_data.get('UnitPrice') or 0)
            list_price = round(line_price_incl / _GST_DIVISOR, 4)

        cost_price = 0.0
        if item:
            cost_price = round(float(item.get('CostPrice') or 0), 4)

        barcode = None
        generic_barcode = None
        if item:
            barcode = (item.get('UPC') or '').strip() or None
            generic_barcode = (item.get('UPC1') or '').strip() or None

        weight = 0.0
        if item:
            weight = round(float(item.get('ShippingWeight') or 0), 4)

        vals = {
            'name':           product_name,
            'default_code':   sku,
            'type':           'consu',
            'sale_ok':        True,
            'purchase_ok':    True,
            'active':         True,
            'list_price':     list_price,
            'standard_price': cost_price,
            'company_id':     False,
            'invoice_policy': 'order',   # always invoice on ordered qty
        }
        if barcode:
            vals['barcode'] = barcode
        if generic_barcode and 'reza_generic_barcode' in Product._fields:
            vals['reza_generic_barcode'] = generic_barcode
        if weight:
            vals['weight'] = weight

        try:
            product = Product.with_context(default_company_id=False).create(vals)
            product.write({'company_id': False})
            template = product.product_tmpl_id
            if 'company_id' in template._fields:
                template.write({'company_id': False})
            if 'company_ids' in template._fields:
                template.write({'company_ids': [(5, 0, 0)]})
            _logger.info(
                'Neto sync: auto-created product "%s" (SKU=%s, price=%.4f, barcode=%s)',
                product_name, sku, list_price, barcode or 'none',
            )
            return product, True
        except Exception as exc:
            _logger.warning(
                'Neto sync: could not auto-create product SKU=%s with full Neto data — %s',
                sku, exc,
            )
            fallback_vals = dict(vals)
            fallback_vals.pop('barcode', None)
            try:
                product = Product.with_context(default_company_id=False).create(fallback_vals)
                product.write({'company_id': False})
                template = product.product_tmpl_id
                if 'company_id' in template._fields:
                    template.write({'company_id': False})
                if 'company_ids' in template._fields:
                    template.write({'company_ids': [(5, 0, 0)]})
                _logger.info(
                    'Neto sync: auto-created fallback product "%s" (SKU=%s)',
                    product_name, sku,
                )
                return product, True
            except Exception as fallback_exc:
                _logger.warning(
                    'Neto sync: fallback product create also failed for SKU=%s — %s',
                    sku, fallback_exc,
                )
                return None, False

    # -------------------------------------------------------------------------
    # API — with pagination
    # -------------------------------------------------------------------------

    def _fetch_orders_page(self, store, since_dt, page=1, until_dt=None):
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
                'Page': page,
                'Limit': _GET_ORDER_PAGE_SIZE,
                'OutputSelector': GET_ORDER_OUTPUT_SELECTOR,
            }
        }
        _logger.info(
            'Neto sync [%s]: POST %s  page=%d', store.name, url, page
        )
        response = requests.post(url, json=payload, headers=headers, timeout=60)
        _logger.info(
            'Neto sync [%s]: HTTP %s  content-length=%s',
            store.name, response.status_code, len(response.content),
        )
        response.raise_for_status()

        body = response.json()
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

        _logger.info(
            'Neto sync [%s]: page %d — %d order(s)',
            store.name, page, len(orders),
        )
        return orders

    def _fetch_orders(self, store, since_dt, until_dt=None):
        _logger.info(
            'Neto sync [%s]: fetching orders updated since %s%s',
            store.name, since_dt.strftime('%Y-%m-%dT%H:%M:%S'),
            f"  until {until_dt.strftime('%Y-%m-%dT%H:%M:%S')}" if until_dt else '',
        )

        all_orders = []
        page = 1
        while True:
            try:
                orders = self._fetch_orders_page(store, since_dt, page=page, until_dt=until_dt)
            except requests.exceptions.RequestException as exc:
                _logger.error(
                    'Neto sync [%s]: API request failed on page %d — %s',
                    store.name, page, exc,
                )
                break
            except Exception as exc:
                _logger.error(
                    'Neto sync [%s]: could not parse/process page %d — %s',
                    store.name, page, exc,
                )
                break
            all_orders.extend(orders)
            if len(orders) < _GET_ORDER_PAGE_SIZE:
                break
            page += 1
        _logger.info(
            'Neto sync [%s]: %d total order(s) across %d page(s)',
            store.name, len(all_orders), page,
        )
        return all_orders

    def _fetch_order_by_id(self, store, order_id):
        url = f"{store.store_url.rstrip('/')}/do/WS/NetoAPI"
        headers = {
            'Content-Type': 'application/json',
            'NETOAPI_ACTION': 'GetOrder',
            'NETOAPI_KEY': store.api_key,
            'Accept': 'application/json',
        }
        payload = {
            'Filter': {
                'OrderID': [order_id],
                'OutputSelector': GET_ORDER_OUTPUT_SELECTOR,
            }
        }
        response = requests.post(url, json=payload, headers=headers, timeout=60)
        response.raise_for_status()
        body = response.json()
        if 'GetOrderResponse' in body:
            orders = body['GetOrderResponse'].get('Order', [])
        else:
            orders = body.get('Order', [])
        if isinstance(orders, dict):
            orders = [orders]
        orders = orders or []
        return orders[0] if orders else None

    # -------------------------------------------------------------------------
    # Payment sync
    # -------------------------------------------------------------------------

    def _fetch_payments_page(self, store, date_paid_from, date_paid_to, page=1):
        url = f"{store.store_url.rstrip('/')}/do/WS/NetoAPI"
        headers = {
            'Content-Type': 'application/json',
            'NETOAPI_ACTION': _PAYMENT_API_ACTION,
            'NETOAPI_KEY': store.api_key,
            'Accept': 'application/json',
        }
        payload = {
            'Filter': {
                'DatePaidFrom': date_paid_from.strftime('%Y-%m-%dT%H:%M:%S'),
                'DatePaidTo': date_paid_to.strftime('%Y-%m-%dT%H:%M:%S'),
                'Page': page,
                'Limit': _GET_PAYMENT_PAGE_SIZE,
                'OutputSelector': GET_PAYMENT_OUTPUT_SELECTOR,
            }
        }
        response = requests.post(url, json=payload, headers=headers, timeout=60)
        response.raise_for_status()
        body = response.json()

        if 'GetPaymentResponse' in body:
            payments = body['GetPaymentResponse'].get('Payment', [])
        else:
            payments = body.get('Payment', [])
        if isinstance(payments, dict):
            payments = [payments]
        payments = payments or []
        _logger.info('Neto payment sync [%s]: page %d — %d payment(s)', store.name, page, len(payments))
        return payments

    def _find_payment_sale_order(self, store, neto_order_id):
        if not neto_order_id:
            return self.env['sale.order']
        domain = [('neto_order_id', '=', neto_order_id)]
        if store.company_id:
            domain.append(('company_id', 'in', [store.company_id.id, False]))
        return self.env['sale.order'].sudo().search(domain, limit=1)

    def _prepare_payment_vals(self, store, payment_data):
        neto_order_id = str(payment_data.get('OrderID') or '').strip()
        sale_order = self._find_payment_sale_order(store, neto_order_id)
        currency = (
            self.env['res.currency'].sudo().search([('name', '=', 'AUD')], limit=1)
            or store.company_id.currency_id
        )
        return {
            'neto_payment_id': str(payment_data.get('PaymentID') or '').strip(),
            'neto_order_id': neto_order_id or False,
            'sale_order_id': sale_order.id or False,
            'partner_id': sale_order.partner_id.id or False,
            'store_id': store.id,
            'company_id': store.company_id.id,
            'amount_paid': float(payment_data.get('AmountPaid') or 0.0),
            'currency_id': currency.id,
            'currency_code': payment_data.get('CurrencyCode') or False,
            'date_paid': self._parse_neto_datetime(payment_data.get('DatePaidUTC')),
            'payment_method': payment_data.get('PaymentMethod') or False,
            'payment_method_name': payment_data.get('PaymentMethodName') or False,
            'process_by': payment_data.get('ProcessBy') or False,
            'payment_notes': payment_data.get('PaymentNotes') or False,
            'is_orphan_payment': not bool(sale_order),
        }

    def _upsert_payment(self, store, payment_data):
        Payment = self.env['neto.payment'].sudo()
        neto_payment_id = str(payment_data.get('PaymentID') or '').strip()
        if not neto_payment_id:
            _logger.warning('Neto payment sync [%s]: skipped payment without PaymentID', store.name)
            return Payment

        vals = self._prepare_payment_vals(store, payment_data)
        existing = Payment.search([('neto_payment_id', '=', neto_payment_id)], limit=1)
        if existing:
            existing.write(vals)
            return existing
        return Payment.create(vals)

    def _relink_orphan_payments(self, store=None):
        domain = [('is_orphan_payment', '=', True), ('neto_order_id', '!=', False)]
        if store:
            domain.append(('store_id', '=', store.id))
        linked = 0
        for payment in self.env['neto.payment'].sudo().search(domain):
            sale_order = self._find_payment_sale_order(payment.store_id, payment.neto_order_id)
            if sale_order:
                payment.write({
                    'sale_order_id': sale_order.id,
                    'partner_id': sale_order.partner_id.id,
                    'is_orphan_payment': False,
                })
                linked += 1
        _logger.info('Neto payment sync: relinked %d orphan payment(s)', linked)
        return linked

    def sync_payments(self, store, date_paid_from, date_paid_to):
        processed = 0
        page = 1
        while True:
            payments = self._fetch_payments_page(store, date_paid_from, date_paid_to, page=page)
            if not payments:
                break
            for payment_data in payments:
                self._upsert_payment(store, payment_data)
                processed += 1
            store.sudo().write({'last_payment_sync_date': fields.Datetime.now()})
            self.env.cr.commit()
            if len(payments) < _GET_PAYMENT_PAGE_SIZE:
                break
            page += 1
        self._relink_orphan_payments(store)
        self.env.cr.commit()
        return processed

    def run_payment_sync_january_2024(self, store_id=None):
        domain = [('active', '=', True)]
        if store_id:
            domain.append(('id', '=', store_id))
        date_paid_from = datetime(2024, 1, 1, 0, 0, 0)
        date_paid_to = datetime(2024, 1, 31, 23, 59, 59)
        total = 0
        for store in self.env['neto.store'].sudo().search(domain):
            total += self.sync_payments(store, date_paid_from, date_paid_to)
        return total

    # -------------------------------------------------------------------------
    # Customer sync
    # -------------------------------------------------------------------------

    def _sync_customer(self, store, partner, username):
        """Pull customer credit/account data from Neto and write it to the Odoo partner."""
        url = f"{store.store_url.rstrip('/')}/do/WS/NetoAPI"
        headers = {
            'Content-Type': 'application/json',
            'NETOAPI_ACTION': 'GetCustomer',
            'NETOAPI_KEY': store.api_key,
            'Accept': 'application/json',
        }
        payload = {
            'Filter': {
                'Username': [username],
                'OutputSelector': [
                    'Username',
                    'AccountBalance',
                    'AvailableCredit',
                    'CreditLimit',
                    'OnCreditHold',
                    'DefaultInvoiceTerms',
                    'AccountManager',
                    'Classification2',
                    'Active',
                ],
            }
        }
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=30)
            response.raise_for_status()
            body = response.json()
        except Exception as exc:
            _logger.warning(
                'Neto sync: GetCustomer failed for username %s — %s', username, exc
            )
            return

        if 'GetCustomerResponse' in body:
            customers = body['GetCustomerResponse'].get('Customer', [])
        else:
            customers = body.get('Customer', [])

        if isinstance(customers, dict):
            customers = [customers]
        customers = customers or []

        if not customers:
            _logger.warning(
                'Neto sync: GetCustomer returned no data for username %s', username
            )
            return

        customer = customers[0]
        vals = {'neto_last_sync': fields.Datetime.now()}

        account_balance = customer.get('AccountBalance')
        if account_balance is not None:
            vals['neto_account_balance'] = str(account_balance)

        available_credit = customer.get('AvailableCredit')
        if available_credit is not None:
            vals['neto_available_credit'] = str(available_credit)

        credit_limit = customer.get('CreditLimit')
        if credit_limit is not None:
            try:
                credit_limit_float = float(credit_limit)
                if 'credit_limit' in self.env['res.partner']._fields:
                    vals['credit_limit'] = credit_limit_float
            except (ValueError, TypeError):
                pass

        on_hold = customer.get('OnCreditHold')
        if on_hold is not None:
            if isinstance(on_hold, bool):
                vals['neto_on_credit_hold'] = on_hold
            else:
                vals['neto_on_credit_hold'] = str(on_hold).lower() in ('true', '1', 'yes')

        invoice_terms = (customer.get('DefaultInvoiceTerms') or '').strip()
        if invoice_terms:
            terms_map = self._find_invoice_terms_map(store, invoice_terms)
            if terms_map:
                vals['property_payment_term_id'] = terms_map.payment_term_id.id
            else:
                _logger.warning(
                    'Neto sync: payment term map "%s" not found for username %s — leaving unchanged',
                    invoice_terms, username,
                )

        account_manager = customer.get('AccountManager')
        if account_manager:
            if isinstance(account_manager, dict):
                manager_email = (account_manager.get('Email') or '').strip()
            else:
                manager_email = ''
            if manager_email:
                user = self.env['res.users'].sudo().search(
                    [('email', '=ilike', manager_email)], limit=1
                )
                if user:
                    vals['user_id'] = user.id
                else:
                    _logger.warning(
                        'Neto sync: account manager email "%s" not found for username %s — leaving unchanged',
                        manager_email, username,
                    )

        classification2 = customer.get('Classification2')
        if classification2 is not None:
            vals['neto_classification'] = str(classification2)

        active = customer.get('Active')
        if active is not None:
            if isinstance(active, bool):
                vals['active'] = active
            else:
                vals['active'] = str(active).strip().lower() in ('true', '1', 'yes')

        try:
            partner.sudo().write(vals)
            _logger.info('Neto sync: GetCustomer synced for username %s', username)
        except Exception as exc:
            _logger.warning(
                'Neto sync: could not write customer fields for username %s — %s',
                username, exc,
            )

    def _find_invoice_terms_map(self, store, invoice_terms):
        normalized_terms = (invoice_terms or '').strip()
        if not normalized_terms:
            return self.env['neto.invoice.terms.map']
        TermsMap = self.env['neto.invoice.terms.map'].sudo()
        domain_base = [
            ('active', '=', True),
            ('neto_invoice_terms', '=ilike', normalized_terms),
        ]
        if store:
            store_map = TermsMap.search(domain_base + [('store_id', '=', store.id)], limit=1)
            if store_map:
                return store_map
        return TermsMap.search(domain_base + [('store_id', '=', False)], limit=1)

    # -------------------------------------------------------------------------
    # Partner
    # -------------------------------------------------------------------------

    def _get_or_create_partner(self, username, order_data, store, synced_customers=None):
        if synced_customers is None:
            synced_customers = set()
        Partner = self.env['res.partner'].sudo().with_context(active_test=False)
        partner = Partner.search([('neto_username', '=', username)], limit=1)
        if partner:
            if username not in synced_customers:
                self._sync_customer(store, partner, username)
                synced_customers.add(username)
            return partner, False

        # Before creating, check if a partner with the same name already exists
        # This prevents duplicates when Odoo already has the customer from another source
        first = (order_data.get("BillFirstName") or "").strip()
        last  = (order_data.get("BillLastName") or "").strip()
        company = (order_data.get("BillCompany") or "").strip()
        if company or (first or last):
            display_name_check = company if company else f"{first} {last}".strip()
            existing = Partner.search([
                ("name", "=", display_name_check),
                ("neto_username", "=", False),
                ("parent_id", "=", False),
            ], limit=1)
            if existing:
                existing.sudo().write({"neto_username": username, "ref": username})
                _logger.info(
                    "Neto sync: linked existing partner %s to username %s",
                    display_name_check, username,
                )
                self._sync_customer(store, existing, username)
                synced_customers.add(username)
                return existing, False

        first   = (order_data.get('BillFirstName') or '').strip()
        last    = (order_data.get('BillLastName')  or '').strip()
        company = (order_data.get('BillCompany')   or '').strip()
        if company:
            display_name = company
        elif first or last:
            display_name = f"{first} {last}".strip()
        else:
            display_name = username

        email = (order_data.get('Email') or '').strip()

        vals = {
            'neto_username': username,
            'ref':           username,
            'name':          display_name,
            'email':         email or False,
            'is_company':    bool(company),
            'company_id':    False,
            'customer_rank': 1,
            'street':        order_data.get('BillStreetLine1') or False,
            'street2':       order_data.get('BillStreetLine2') or False,
            'city':          order_data.get('BillCity')        or False,
            'zip':           order_data.get('BillPostCode')    or False,
            'phone':         order_data.get('BillPhone')       or False,
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
        self._sync_customer(store, partner, username)
        synced_customers.add(username)
        return partner, True

    # -------------------------------------------------------------------------
    # Invoice creation
    # -------------------------------------------------------------------------

    def _get_payment_journal(self, payment_method, company=None):
        """Return an account.journal for the given payment method name."""
        Journal = self.env['account.journal'].sudo()
        domain = [('type', 'in', ('bank', 'cash'))]
        if company:
            domain.append(('company_id', '=', company.id))
        if payment_method:
            journal = Journal.search(
                domain + [('name', 'ilike', payment_method)],
                limit=1,
            )
            if journal:
                _logger.info(
                    'Neto sync: payment journal "%s" selected for method "%s"%s',
                    journal.name, payment_method,
                    ' in company "%s"' % company.display_name if company else '',
                )
                return journal
            _logger.warning(
                'Neto sync: no journal matching "%s"%s — using first available bank/cash journal',
                payment_method,
                ' in company "%s"' % company.display_name if company else '',
            )
        journal = Journal.search(domain, limit=1)
        if journal:
            _logger.info('Neto sync: fallback payment journal "%s" selected', journal.name)
        return journal

    def _create_invoice(self, order, payment_method, date_paid, amount_paid=None):
        """Create, post, and optionally pay an invoice for a confirmed sale order."""
        try:
            order.sudo()._create_invoices()
            if not order.invoice_ids:
                _logger.warning(
                    'Neto sync: no invoice created for order %s', order.neto_order_id
                )
                return
            invoice = order.invoice_ids[0]
            invoice.sudo().write({'invoice_date': order.date_order})
            invoice.sudo().action_post()
            _logger.info(
                'Neto sync: invoice %s posted for order %s',
                invoice.name, order.neto_order_id,
            )

            if date_paid and payment_method:
                journal = self._get_payment_journal(payment_method, order.company_id)
                if not journal:
                    _logger.warning(
                        'Neto sync: no payment journal found for order %s — skipping payment registration',
                        order.neto_order_id,
                    )
                    return

                # Use actual amount paid from Neto rather than invoice total
                # This handles partial payments correctly
                pay_amount = amount_paid if amount_paid else invoice.amount_total

                payment = self.env['account.payment'].sudo().with_company(order.company_id).create({
                    'payment_type':  'inbound',
                    'partner_type':  'customer',
                    'partner_id':    order.partner_id.id,
                    'amount':        pay_amount,
                    'date':          date_paid,
                    'journal_id':    journal.id,
                    'company_id':    order.company_id.id,
                })
                payment.sudo().action_post()
                payment.sudo().invalidate_recordset()

                payment_lines = payment.move_id.line_ids.filtered(
                    lambda l: l.account_id.account_type == 'asset_receivable'
                )
                invoice_lines = invoice.line_ids.filtered(
                    lambda l: l.account_id.account_type == 'asset_receivable'
                    and not l.reconciled
                )
                (payment_lines + invoice_lines).reconcile()
                _logger.info(
                    'Neto sync: payment registered and reconciled for order %s '
                    '(amount=%.2f, method=%s)',
                    order.neto_order_id, pay_amount, payment_method,
                )
        except Exception as exc:
            _logger.warning(
                'Neto sync: could not create invoice/payment for order %s — %s',
                order.neto_order_id, exc,
            )

    # -------------------------------------------------------------------------
    # Order creation / state management
    # -------------------------------------------------------------------------

    def _set_order_state(self, order, order_id, order_status, date_order, line_prices, import_as_history=False):
        """Set the final state of a synced sale order."""
        if import_as_history:
            target_state = 'draft'
        elif order_status in _CANCEL_STATUSES:
            target_state = 'cancel'
        else:
            target_state = 'sale'

        writes = {'state': target_state}
        if date_order:
            writes['date_order'] = date_order

        order.sudo().write(writes)

        if target_state == 'sale':
            for ol in order.order_line:
                neto = line_prices.get(ol.product_id.id)
                if neto is not None:
                    price, disc = neto
                    price_writes = {}
                    if ol.price_unit != price:
                        price_writes['price_unit'] = price
                    if ol.discount != disc:
                        price_writes['discount'] = disc
                    if price_writes:
                        ol.sudo().write(price_writes)

        _logger.info(
            'Neto sync: order %s — state set to "%s" (Neto status: %s)',
            order_id, target_state, order_status,
        )
        return target_state

    def _patch_existing_order_from_neto(self, order, order_data, store, synced_customers=None):
        """Refresh Neto metadata on an existing order without touching workflow records."""
        if synced_customers is None:
            synced_customers = set()
        order_id = order_data.get('OrderID', '') or order.neto_order_id
        username = order_data.get('Username', '') or ''
        order_status = order_data.get('OrderStatus', '') or ''
        payment_method, _amount_paid, date_paid, _is_partial = self._get_payment_info_from_order(
            order_data, order_id
        )

        write_vals = {}
        if order_status:
            write_vals['neto_order_status'] = order_status
        if 'neto_consignment_number' in order._fields:
            write_vals['neto_consignment_number'] = self._get_consignment_number_from_order(order_data)
        if date_paid and 'neto_date_paid' in order._fields:
            write_vals['neto_date_paid'] = date_paid
        if payment_method and 'neto_payment_method' in order._fields:
            write_vals['neto_payment_method'] = payment_method

        ship_partner = self._get_or_create_ship_address(order.partner_id, order_data)
        if ship_partner:
            write_vals['partner_shipping_id'] = ship_partner.id

        if write_vals:
            order.sudo().write(write_vals)

        if username and username not in synced_customers:
            self._sync_customer(store, order.partner_id, username)
            synced_customers.add(username)

        _logger.info(
            'Neto sync: refreshed existing order %s for Neto order %s — fields updated: %s',
            order.name, order_id, sorted(write_vals.keys()),
        )
        return write_vals

    def _create_sale_order(self, order_data, partner, store, neto_internal=False, import_as_history=False):
        Order = self.env['sale.order'].sudo()

        order_id = order_data.get('OrderID', '')

        date_order = (
            self._parse_neto_datetime(order_data.get('DatePlaced'))
            or fields.Datetime.now()
        )

        # --- Payment info from OrderPayment block — no extra API call needed ---
        payment_method, amount_paid, date_paid, is_partial = \
            self._get_payment_info_from_order(order_data, order_id)

        ship_partner = self._get_or_create_ship_address(partner, order_data)
        order_status = order_data.get('OrderStatus', '') or ''

        order_vals = {
            'partner_id':           partner.id,
            'partner_shipping_id':  ship_partner.id,
            'neto_order_id':        order_id,
            'neto_order_status':    order_status,
            'neto_consignment_number': self._get_consignment_number_from_order(order_data),
            'neto_internal':        neto_internal,
            'neto_history_import':  import_as_history,
            'date_order':           date_order,
            'warehouse_id':         store.warehouse_id.id,
            'company_id':           store.company_id.id,
        }
        if date_paid and 'neto_date_paid' in self.env['sale.order']._fields:
            order_vals['neto_date_paid'] = date_paid
        if payment_method and 'neto_payment_method' in self.env['sale.order']._fields:
            order_vals['neto_payment_method'] = payment_method

        order = Order.create(order_vals)

        raw_lines = order_data.get('OrderLine', [])
        if isinstance(raw_lines, dict):
            raw_lines = [raw_lines]

        line_prices     = {}
        missing_lines   = []
        autocreated_lines = []
        note_lines      = []

        for line in raw_lines:
            sku = (line.get('SKU') or line.get('Sku') or '').strip()
            if not sku:
                _logger.warning(
                    'Neto sync: order %s has a line with no SKU — skipping line', order_id
                )
                continue

            if self._is_note_sku(sku):
                note_text = (line.get('ProductName') or '').strip()
                _logger.info(
                    'Neto sync: order %s — TEXT_NOTE collected for chatter: %r',
                    order_id, note_text,
                )
                note_lines.append(note_text)
                continue

            if self._is_internal_sku(sku):
                _logger.info(
                    'Neto sync: order %s — silently dropped internal SKU "%s"',
                    order_id, sku,
                )
                continue

            added_line, missing_line = self._add_neto_order_line(
                order, store, order_data, line
            )
            if missing_line:
                _logger.warning(
                    'Neto sync: SKU "%s" could not be found or created for order %s — skipping line',
                    sku, order_id,
                )
                missing_lines.append(missing_line)
                continue
            if not added_line:
                continue
            if added_line.get('was_autocreated'):
                autocreated_lines.append(added_line)
            order_line = added_line['order_line']
            line_prices[order_line.product_id.id] = (
                order_line.price_unit,
                order_line.discount,
            )

        self._add_neto_total_lines(order, order_data)

        # --- Set final order state ---
        final_state = self._set_order_state(
            order, order_id, order_status, date_order, line_prices,
            import_as_history=import_as_history,
        )

        # --- Invoice creation logic ---
        if final_state == 'sale':
            cutoff = fields.Datetime.now() - timedelta(days=_INVOICE_CUTOFF_DAYS)
            is_recent = date_order and date_order >= cutoff
            is_paid = self._is_fully_paid(order_data)

            if is_recent or is_paid:
                if not is_recent and is_paid:
                    _logger.info(
                        'Neto sync: order %s is older than %d days but fully paid — creating invoice',
                        order_id, _INVOICE_CUTOFF_DAYS,
                    )
                self._create_invoice(order, payment_method, date_paid, amount_paid=amount_paid)
            else:
                _logger.info(
                    'Neto sync: order %s is older than %d days and unpaid — skipping invoice',
                    order_id, _INVOICE_CUTOFF_DAYS,
                )
        elif import_as_history:
            _logger.info(
                'Neto sync: order %s imported in history mode — left as quotation and skipped invoice creation',
                order_id,
            )

        # -----------------------------------------------------------------------
        # Post chatter message
        # -----------------------------------------------------------------------
        msg_parts = []

        if neto_internal:
            msg_parts.append(Markup('<p>⚠️ No billable items.</p>'))

        if import_as_history:
            msg_parts.append(Markup(
                '<p>&#128221; <strong>Imported as history quotation.</strong> '
                'This Neto order was intentionally left unconfirmed so it does not affect '
                'outstanding amounts or live sales workflow.</p>'
            ))

        if order_status in _CANCEL_STATUSES:
            msg_parts.append(Markup(
                '<p>&#10060; <strong>This order was automatically cancelled</strong> '
                'because its Neto status is <em>{status}</em>.</p>'
            ).format(status=order_status))

        if order_status in _DISPATCHED_STATUSES:
            msg_parts.append(Markup(
                '<p>&#128666; <strong>This order has been dispatched in Neto.</strong> '
                'It is confirmed in Odoo. Neto Status: <em>Dispatched</em>.</p>'
            ))

        if is_partial and amount_paid:
            msg_parts.append(Markup(
                '<p>&#9888;&#65039; <strong>Partial payment recorded in Neto: '
                '${paid} of ${total} paid.</strong></p>'
            ).format(
                paid=f"{amount_paid:.2f}",
                total=order_data.get('GrandTotal', '?'),
            ))

        if autocreated_lines:
            rows = Markup('').join(
                Markup(
                    '<tr style="border-bottom:1px solid #e0e0e0;">'
                    '<td style="padding:4px 10px;font-family:monospace;">{sku}</td>'
                    '<td style="padding:4px 10px;">{name}</td>'
                    '<td style="padding:4px 10px;text-align:center;">{qty}</td>'
                    '<td style="padding:4px 10px;text-align:right;">${price} '
                    '<small style="color:#888;">(GST-inc)</small></td>'
                    '</tr>'
                ).format(sku=m['sku'], name=m['name'], qty=m['qty'], price=m['price'])
                for m in autocreated_lines
            )
            msg_parts.append(Markup(
                '<p>&#9989; <strong>The following products were <u>auto-created</u> from Neto '
                '(marked <em>[NETO-UNSYNCED]</em>) and added to this order. '
                'Please review and update them in the product catalog:</strong></p>'
                '<table style="border-collapse:collapse;width:100%;font-size:13px;">'
                '<thead><tr style="background:#f0fff4;font-weight:600;">'
                '<th style="padding:5px 10px;text-align:left;">SKU</th>'
                '<th style="padding:5px 10px;">Product Name</th>'
                '<th style="padding:5px 10px;text-align:center;">Qty</th>'
                '<th style="padding:5px 10px;text-align:right;">Unit Price</th>'
                '</tr></thead><tbody>{rows}</tbody></table>'
            ).format(rows=rows))

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
                ).format(sku=m['sku'], name=m['name'], qty=m['qty'], price=m['price'])
                for m in missing_lines
            )
            msg_parts.append(Markup(
                '<p>&#9888;&#65039; <strong>The following Neto lines could not be matched or '
                'created in Odoo and were <u>NOT</u> added to this order:</strong></p>'
                '<table style="border-collapse:collapse;width:100%;font-size:13px;">'
                '<thead><tr style="background:#f5f5f5;font-weight:600;">'
                '<th style="padding:5px 10px;text-align:left;">SKU</th>'
                '<th style="padding:5px 10px;">Product Name</th>'
                '<th style="padding:5px 10px;text-align:center;">Qty</th>'
                '<th style="padding:5px 10px;text-align:right;">Unit Price</th>'
                '</tr></thead><tbody>{rows}</tbody></table>'
            ).format(rows=rows))

        if note_lines:
            note_items = Markup('').join(
                Markup('<li style="margin:2px 0;">{text}</li>').format(text=n)
                for n in note_lines
            )
            msg_parts.append(Markup(
                '<p style="margin-top:12px;">&#8505;&#65039; <strong>Neto order notes '
                '(TEXT_NOTE lines — FYI only):</strong></p>'
                '<ul style="margin:4px 0 0 16px;font-size:13px;color:#555;">'
                '{items}</ul>'
            ).format(items=note_items))

        if msg_parts:
            order.sudo().message_post(body=Markup('').join(msg_parts))

        return order, missing_lines

    def _line_sku_set(self, order):
        return {
            (line.product_id.default_code or '').strip()
            for line in order.order_line
            if line.product_id and line.product_id.default_code
        }

    def _add_neto_order_line(self, order, store, order_data, line, existing_skus=None):
        sku = (line.get('SKU') or line.get('Sku') or '').strip()
        if not sku or self._is_note_sku(sku) or self._is_internal_sku(sku):
            return False, False
        if existing_skus is not None and sku in existing_skus:
            return False, False

        product, was_autocreated = self._get_or_create_product_from_neto(store, sku, line)
        if not product:
            return False, {
                'sku': sku,
                'name': line.get('ProductName') or '',
                'qty': line.get('Quantity') or '',
                'price': f"{float(line.get('UnitPrice') or 0):.2f}",
            }

        neto_price_incl = float(line.get('UnitPrice') or 0)
        neto_price_excl = round(neto_price_incl / _GST_DIVISOR, 4)
        qty = float(line.get('Quantity') or 1)

        percent_discount = float(line.get('PercentDiscount') or 0)
        if not percent_discount:
            product_discount_amt = float(line.get('ProductDiscount') or 0)
            line_total_incl = neto_price_incl * qty
            if product_discount_amt and line_total_incl:
                percent_discount = round(product_discount_amt / line_total_incl * 100, 4)

        try:
            line_description = product.with_context(display_default_code=False).display_name
            order_line = self.env['sale.order.line'].sudo().create({
                'order_id': order.id,
                'product_id': product.id,
                'product_uom_qty': qty,
                'price_unit': neto_price_excl,
                'discount': percent_discount,
                'name': line_description,
                'product_uom_id': product.uom_id.id,
            })
        except Exception as line_exc:
            _logger.warning(
                'Neto sync: could not create line SKU=%s on order %s — %s',
                sku, order.name, line_exc,
            )
            return False, {
                'sku': sku,
                'name': line.get('ProductName') or '',
                'qty': line.get('Quantity') or '',
                'price': f"{float(line.get('UnitPrice') or 0):.2f}",
            }
        if existing_skus is not None:
            existing_skus.add(sku)
        return {
            'sku': sku,
            'name': product.name,
            'qty': line.get('Quantity') or '',
            'price': f"{float(line.get('UnitPrice') or 0):.2f}",
            'was_autocreated': was_autocreated,
            'order_line': order_line,
        }, False

    def repair_missing_sku_lines(self, sync_logs=None, limit=50):
        SyncLog = self.env['neto.sync.log'].sudo()
        if sync_logs:
            logs = sync_logs.sudo()
        else:
            logs = SyncLog.search([
                '&',
                ('sale_order_id', '!=', False),
                '|',
                ('missing_skus', '!=', False),
                ('neto_total_lines_checked', '=', False),
            ], order='sync_date asc', limit=limit)

        repaired_orders = 0
        added_lines = 0
        still_missing_orders = 0
        for log in logs:
            order = log.sale_order_id
            store = log.store_id
            if not order or not store:
                continue
            order_data = self._fetch_order_by_id(store, log.neto_order_id)
            if not order_data:
                continue
            raw_lines = order_data.get('OrderLine', [])
            if isinstance(raw_lines, dict):
                raw_lines = [raw_lines]

            wanted_skus = {
                sku.strip()
                for sku in (log.missing_skus or '').split(',')
                if sku.strip()
            }
            existing_skus = self._line_sku_set(order)
            still_missing = []
            added = []
            for line in raw_lines:
                sku = (line.get('SKU') or line.get('Sku') or '').strip()
                if sku not in wanted_skus:
                    continue
                added_line, missing_line = self._add_neto_order_line(
                    order, store, order_data, line, existing_skus=existing_skus
                )
                if added_line:
                    added.append(added_line)
                    added_lines += 1
                elif missing_line:
                    still_missing.append(missing_line['sku'])

            total_lines = self._add_neto_total_lines(
                order, order_data, existing_skus=existing_skus
            )
            if added:
                repaired_orders += 1
            if total_lines:
                added_lines += len(total_lines)
                if not added:
                    repaired_orders += 1
                added.extend({
                    'sku': line.product_id.default_code or line.name,
                } for line in total_lines)
            if added:
                order.sudo().message_post(body=Markup(
                    '<p>&#9989; <strong>Repaired Neto order lines/totals:</strong> {skus}</p>'
                ).format(skus=', '.join(a['sku'] for a in added)))
            log.write({
                'missing_skus': ', '.join(still_missing) if still_missing else False,
                'line_count': len(order.order_line),
                'neto_total_lines_checked': True,
            })
            if still_missing:
                still_missing_orders += 1
            self.env.cr.commit()

        return {
            'logs_checked': len(logs),
            'orders_repaired': repaired_orders,
            'lines_added': added_lines,
            'orders_still_missing': still_missing_orders,
        }

    # -------------------------------------------------------------------------
    # Per-order processing
    # -------------------------------------------------------------------------

    def _process_order(self, order_data, store, synced_ids, synced_customers=None, import_as_history=False):
        if synced_customers is None:
            synced_customers = set()
        SyncLog = self.env['neto.sync.log'].sudo()

        order_id     = order_data.get('OrderID', '')
        username     = order_data.get('Username', '') or ''
        billing_email = (order_data.get('Email') or '').strip()
        grand_total  = float(order_data.get('GrandTotal') or 0)
        order_status = order_data.get('OrderStatus', '') or ''
        neto_order_date = self._parse_neto_datetime(order_data.get('DatePlaced'))

        base_vals = {
            'neto_order_id':     order_id,
            'neto_username':     username,
            'neto_order_date':   neto_order_date,
            'neto_grand_total':  grand_total,
            'neto_order_status': order_status,
            'store_id':          store.id,
        }

        try:
            if order_id in synced_ids:
                return
            existing_order = self.env['sale.order'].sudo().search(
                [('neto_order_id', '=', order_id)], limit=1
            )
            if existing_order:
                updated_fields = self._patch_existing_order_from_neto(
                    existing_order, order_data, store, synced_customers
                )
                SyncLog.create({
                    **base_vals,
                    'state': 'success',
                    'sale_order_id': existing_order.id,
                    'partner_id': existing_order.partner_id.id,
                    'line_count': len(existing_order.order_line),
                    'neto_total_lines_checked': True,
                    'skip_reason': (
                        'Existing order refreshed from Neto; updated fields: %s'
                        % ', '.join(sorted(updated_fields.keys()))
                    ),
                })
                return
            synced_ids.add(order_id)

            neto_internal = (
                grand_total == 0
                or _INTERNAL_EMAIL_DOMAIN in billing_email.lower()
            )

            reason = False
            if neto_internal:
                if grand_total == 0:
                    reason = 'Zero-value internal transfer (synced, flagged internal)'
                else:
                    reason = 'BrightEyes internal replenishment (synced, flagged internal)'
                _logger.info('Neto sync: order %s — %s', order_id, reason)

            partner, partner_created = self._get_or_create_partner(
                username, order_data, store, synced_customers
            )
            partner = self._ensure_partner_company_compatibility(
                partner, store, username=username,
            )
            sale_order, missing_lines = self._create_sale_order(
                order_data, partner, store,
                neto_internal=neto_internal,
                import_as_history=import_as_history,
            )
            line_count = len(sale_order.order_line)
            missing_skus_text = ', '.join(m['sku'] for m in missing_lines) if missing_lines else False

            SyncLog.create({
                **base_vals,
                'state':           'success',
                'sale_order_id':   sale_order.id,
                'partner_id':      partner.id,
                'partner_created': partner_created,
                'line_count':      line_count,
                'missing_skus':    missing_skus_text,
                'neto_total_lines_checked': True,
                'skip_reason':     reason,
            })

        except Exception as exc:
            _logger.exception('Neto sync: unhandled error on order %s', order_id)
            SyncLog.create({**base_vals, 'state': 'error', 'error_message': str(exc)})

    # -------------------------------------------------------------------------
    # Per-store sync
    # -------------------------------------------------------------------------

    def _disable_unsafe_temp_history_cron(self):
        cron_id = self.env.context.get('cron_id')
        if not cron_id:
            return False
        cron = self.env['ir.cron'].sudo().browse(cron_id).exists()
        if not cron or cron.name != 'TEMP Liaise Neto History Sync 2024':
            return False
        cron.write({'active': False})
        _logger.warning(
            'Neto sync: disabled unsafe temp history cron "%s"; '
            'use the reviewed chunked history process instead.',
            cron.name,
        )
        return True

    def _sync_store(
        self,
        store,
        hours_back=None,
        since_dt=None,
        until_dt=None,
        import_as_history=False,
        should_stop=None,
        update_cursor=True,
    ):
        # Suppress all email notifications during sync
        self = self.with_context(mail_notrack=True, mail_create_nosubscribe=True, tracking_disable=True)
        if self._disable_unsafe_temp_history_cron():
            return
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
            'Neto sync [%s]: fetching orders updated since %s%s%s',
            store.name, since_dt,
            f' until {until_dt}' if until_dt else '',
            ' [history quotations mode]' if import_as_history else '',
        )

        synced_ids = set()
        synced_customers = set()
        processed_orders = 0
        page = 1

        while True:
            if should_stop and should_stop():
                _logger.info(
                    'Neto sync [%s]: stopped before page %d by cancel request',
                    store.name, page,
                )
                break
            try:
                orders = self._fetch_orders_page(store, since_dt, page=page, until_dt=until_dt)
            except requests.exceptions.RequestException as exc:
                _logger.error(
                    'Neto sync [%s]: API request failed on page %d — %s',
                    store.name, page, exc,
                )
                break
            except Exception as exc:
                _logger.error(
                    'Neto sync [%s]: _fetch_orders_page failed on page %d — %s',
                    store.name, page, exc,
                )
                break

            if not orders:
                break

            _logger.info(
                'Neto sync [%s]: processing %d order(s) from page %d',
                store.name, len(orders), page,
            )
            for order_data in orders:
                if should_stop and should_stop():
                    _logger.info(
                        'Neto sync [%s]: stopped during page %d by cancel request',
                        store.name, page,
                    )
                    break
                self._process_order(
                    order_data, store, synced_ids, synced_customers,
                    import_as_history=import_as_history,
                )
                processed_orders += 1

            if update_cursor:
                store.sudo().write({'last_sync_date': fields.Datetime.now()})
            self.env.cr.commit()

            if should_stop and should_stop():
                _logger.info(
                    'Neto sync [%s]: stopped after page %d by cancel request',
                    store.name, page,
                )
                break

            if len(orders) < _GET_ORDER_PAGE_SIZE:
                break
            page += 1

        _logger.info(
            'Neto sync [%s]: completed — %d order(s) processed across %d page(s).',
            store.name, processed_orders, page,
        )

    # -------------------------------------------------------------------------
    # Public entry point (cron)
    # -------------------------------------------------------------------------

    def run_sync(self, hours_back=None, import_as_history=False):
        stores = self.env['neto.store'].sudo().search([('active', '=', True)])
        _logger.info(
            'Neto connector: run_sync called — %d active store(s) found%s',
            len(stores),
            ' [history quotations mode]' if import_as_history else '',
        )
        if not stores:
            _logger.warning('Neto connector: no active stores configured — aborting sync.')
            return
        for store in stores:
            try:
                self._sync_store(
                    store,
                    hours_back=hours_back,
                    import_as_history=import_as_history,
                )
            except Exception as exc:
                _logger.exception(
                    'Neto connector: _sync_store failed for store "%s" — %s',
                    store.name, exc,
                )
            try:
                rma_since = store.last_rma_sync_date
                if rma_since:
                    rma_since = rma_since.replace(tzinfo=timezone.utc)
                else:
                    rma_since = datetime.now(timezone.utc) - timedelta(hours=24)
                self._sync_rmas(store, rma_since)
            except Exception as exc:
                _logger.exception(
                    'Neto connector: _sync_rmas failed for store "%s" — %s',
                    store.name, exc,
                )

    # -------------------------------------------------------------------------
    # RMA sync
    # -------------------------------------------------------------------------

    _GET_RMA_OUTPUT_SELECTOR = [
        'RmaID', 'OrderID', 'InvoiceNumber', 'CustomerUsername',
        'RmaStatus', 'InternalNotes',
        'ShippingRefundAmount', 'SurchargeRefundAmount',
        'RefundSubtotal', 'RefundTotal', 'RefundTaxTotal',
        'DateIssued', 'DateUpdated', 'DateApproved',
        'RmaLine', 'RmaLine.SKU', 'RmaLine.ProductName',
        'RmaLine.Quantity', 'RmaLine.RefundSubtotal',
        'RmaLine.Tax', 'RmaLine.TaxCode', 'RmaLine.ReturnReason',
        'Refund', 'Refund.PaymentMethodName',
        'Refund.RefundAmount', 'Refund.DateRefunded',
        'Refund.RefundStatus', 'RefundedTotal',
    ]

    def _fetch_rmas(self, store, since_dt, until_dt=None):
        url = f"{store.store_url.rstrip('/')}/do/WS/NetoAPI"
        headers = {
            'Content-Type': 'application/json',
            'NETOAPI_ACTION': 'GetRma',
            'NETOAPI_KEY': store.api_key,
            'Accept': 'application/json',
        }
        date_filter = {'DateUpdatedFrom': since_dt.strftime('%Y-%m-%dT%H:%M:%S')}
        if until_dt:
            date_filter['DateUpdatedTo'] = until_dt.strftime('%Y-%m-%dT%H:%M:%S')

        all_rmas = []
        page = 1
        while True:
            payload = {
                'Filter': {
                    **date_filter,
                    'Page': page,
                    'Limit': 50,
                    'OutputSelector': self._GET_RMA_OUTPUT_SELECTOR,
                }
            }
            try:
                response = requests.post(url, json=payload, headers=headers, timeout=60)
                response.raise_for_status()
                body = response.json()
            except Exception as exc:
                _logger.error('Neto RMA sync [%s]: API error page %d — %s', store.name, page, exc)
                break

            rmas = body.get('Rma', [])
            if isinstance(rmas, dict):
                rmas = [rmas]
            rmas = rmas or []

            _logger.info('Neto RMA sync [%s]: page %d — %d RMA(s)', store.name, page, len(rmas))
            all_rmas.extend(rmas)

            if len(rmas) < 50:
                break
            page += 1

        _logger.info('Neto RMA sync [%s]: %d total RMA(s)', store.name, len(all_rmas))
        return all_rmas

    def _get_refunds_from_rma(self, rma_data):
        refunds = rma_data.get('Refunds') or rma_data.get('Refund') or []
        if not refunds or refunds == '':
            return []
        if isinstance(refunds, dict):
            inner = refunds.get('Refund', refunds)
            if isinstance(inner, dict):
                return [inner]
            return inner or []
        if isinstance(refunds, list):
            return refunds
        return []

    def _create_credit_note(self, rma_data, partner, store, original_invoice=None):
        Move = self.env['account.move'].sudo()
        MoveLine = self.env['account.move.line'].sudo()
        Product = self.env['product.product'].sudo()

        rma_id = str(rma_data.get('RmaID', ''))
        invoice_number = (rma_data.get('InvoiceNumber') or '').strip()
        rma_status = (rma_data.get('RmaStatus') or '').strip()
        internal_notes = (rma_data.get('InternalNotes') or '').strip()

        # Use _parse_neto_date to slice YYYY-MM-DD directly from the raw string,
        # avoiding UTC conversion that rolls AEST dates back by one day.
        date_issued = self._parse_neto_date(rma_data.get('DateIssued'))

        move_vals = {
            'move_type':              'out_refund',
            'partner_id':             partner.id,
            'company_id':             store.company_id.id,
            'invoice_date':           date_issued or fields.Date.today(),
            'invoice_payment_term_id': False,  # immediate due date — do not inherit customer terms
            'ref':                    f"Neto RMA {rma_id} / {invoice_number}",
            'narration':              internal_notes or False,
            'neto_rma_id':            rma_id,
            'neto_rma_status':        rma_status,
        }
        if original_invoice:
            move_vals['invoice_origin'] = original_invoice.name

        credit_note = Move.create(move_vals)

        raw_lines = rma_data.get('RmaLines') or {}
        if isinstance(raw_lines, dict):
            raw_lines = raw_lines.get('RmaLine', [])
        if isinstance(raw_lines, dict):
            raw_lines = [raw_lines]
        raw_lines = raw_lines or []

        note_lines = []

        for line in raw_lines:
            sku = (line.get('SKU') or '').strip()
            product_name = (line.get('ProductName') or '').strip()
            refund_subtotal = round(float(line.get('RefundSubtotal') or 0), 4)
            qty = float(line.get('Quantity') or 1) or 1
            return_reason = (line.get('ReturnReason') or '').strip()

            if sku == 'TEXT_NOTE' or (not sku and product_name.startswith('NOTE:')):
                note_lines.append(product_name)
                continue

            if not refund_subtotal and not product_name:
                continue

            product = None
            if sku:
                product = Product.search([('default_code', '=', sku)], limit=1)
                if not product:
                    product = Product.search([('barcode', '=', sku)], limit=1)
            if not product and product_name:
                product = Product.search([('name', 'ilike', product_name)], limit=1)

            description = product_name or (product.name if product else sku or 'Return')
            if return_reason and return_reason.lower() != 'other':
                description += f' — Reason: {return_reason}'

            # Neto RefundSubtotal is GST-inclusive; divide by GST_DIVISOR so
            # Odoo adds tax correctly without double-counting GST.
            refund_subtotal_excl = round(refund_subtotal / _GST_DIVISOR, 4)
            line_vals = {
                'move_id':    credit_note.id,
                'quantity':   qty,
                'price_unit': round(refund_subtotal_excl / qty, 4) if qty else refund_subtotal_excl,
                'name':       description,
            }
            if product:
                line_vals['product_id'] = product.id

            try:
                MoveLine.create(line_vals)
            except Exception as exc:
                _logger.warning(
                    'Neto RMA sync: could not create line SKU=%s RMA=%s — %s',
                    sku, rma_id, exc,
                )

        shipping_refund = round(float(rma_data.get('ShippingRefundAmount') or 0), 4)
        if shipping_refund > 0:
            ship_product = self._get_shipping_product()
            if ship_product:
                try:
                    MoveLine.create({
                        'move_id':    credit_note.id,
                        'product_id': ship_product.id,
                        'quantity':   1,
                        'price_unit': round(shipping_refund / _GST_DIVISOR, 4),
                        'name':       'Shipping Refund',
                    })
                except Exception as exc:
                    _logger.warning('Neto RMA sync: shipping line failed RMA=%s — %s', rma_id, exc)

        surcharge_refund = round(float(rma_data.get('SurchargeRefundAmount') or 0), 4)
        if surcharge_refund > 0:
            sc_product = self._get_surcharge_product()
            try:
                MoveLine.create({
                    'move_id':    credit_note.id,
                    'product_id': sc_product.id,
                    'quantity':   1,
                    'price_unit': round(surcharge_refund / _GST_DIVISOR, 4),
                    'name':       'Surcharge Refund',
                })
            except Exception as exc:
                _logger.warning('Neto RMA sync: surcharge line failed RMA=%s — %s', rma_id, exc)

        try:
            credit_note.sudo().action_post()
            _logger.info('Neto RMA sync: credit note %s posted for RMA %s', credit_note.name, rma_id)
        except Exception as exc:
            _logger.warning('Neto RMA sync: could not post credit note RMA=%s — %s', rma_id, exc)
            return credit_note, note_lines

        refunds = self._get_refunds_from_rma(rma_data)
        for refund in refunds:
            if (refund.get('RefundStatus') or '').strip() != 'Refunded':
                continue
            refund_amount = round(float(refund.get('RefundAmount') or 0), 2)
            date_refunded = self._parse_neto_datetime(refund.get('DateRefunded'))
            payment_method = (refund.get('PaymentMethodName') or '').strip() or None
            if not refund_amount or not date_refunded:
                continue
            journal = self._get_payment_journal(payment_method, store.company_id)
            if not journal:
                continue
            try:
                payment = self.env['account.payment'].sudo().with_company(store.company_id).create({
                    'payment_type': 'outbound',
                    'partner_type': 'customer',
                    'partner_id':   partner.id,
                    'amount':       refund_amount,
                    'date':         date_refunded,
                    'journal_id':   journal.id,
                    'company_id':   store.company_id.id,
                })
                payment.sudo().action_post()
                payment.sudo().invalidate_recordset()
                payment_lines = payment.move_id.line_ids.filtered(
                    lambda l: l.account_id.account_type == 'asset_receivable'
                )
                credit_lines = credit_note.line_ids.filtered(
                    lambda l: l.account_id.account_type == 'asset_receivable'
                    and not l.reconciled
                )
                (payment_lines + credit_lines).reconcile()
                _logger.info(
                    'Neto RMA sync: refund payment %.2f reconciled for RMA %s',
                    refund_amount, rma_id,
                )
            except Exception as exc:
                _logger.warning(
                    'Neto RMA sync: refund payment failed RMA=%s — %s', rma_id, exc,
                )

        return credit_note, note_lines

    def _process_rma(self, rma_data, store, synced_rma_ids):
        RmaLog = self.env['neto.rma.log'].sudo()

        rma_id = str(rma_data.get('RmaID', ''))
        invoice_number = (rma_data.get('InvoiceNumber') or '').strip()
        order_id_field = (rma_data.get('OrderID') or '').strip()
        username = (rma_data.get('CustomerUsername') or '').strip()
        rma_status = (rma_data.get('RmaStatus') or '').strip()
        refund_total = round(float(rma_data.get('RefundTotal') or 0), 2)

        base_vals = {
            'neto_rma_id':        rma_id,
            'neto_invoice_number': invoice_number,
            'neto_username':      username,
            'neto_rma_status':    rma_status,
            'neto_refund_total':  refund_total,
            'store_id':           store.id,
        }

        try:
            if rma_id in synced_rma_ids:
                return
            if self.env['account.move'].sudo().search_count(
                [('neto_rma_id', '=', rma_id)]
            ):
                return
            synced_rma_ids.add(rma_id)

            lookup_id = order_id_field or invoice_number
            original_order = None
            original_invoice = None

            if lookup_id:
                original_order = self.env['sale.order'].sudo().search(
                    [('neto_order_id', '=', lookup_id)], limit=1
                )
                if not original_order:
                    _logger.info(
                        'Neto RMA sync: order %s not found — syncing first', lookup_id
                    )
                    self._sync_single_order_by_id(store, lookup_id)
                    original_order = self.env['sale.order'].sudo().search(
                        [('neto_order_id', '=', lookup_id)], limit=1
                    )
                if not original_order:
                    _logger.info(
                        'Neto RMA sync: RMA %s — order %s not found after sync attempt, '
                        'logging as skipped', rma_id, lookup_id
                    )
                    RmaLog.create({
                        **base_vals,
                        'state': 'skipped',
                        'skip_reason': f'Order {lookup_id} not found in Odoo (outside sync window)',
                    })
                    return

            if original_order and original_order.invoice_ids:
                original_invoice = original_order.invoice_ids.filtered(
                    lambda i: i.move_type == 'out_invoice' and i.state == 'posted'
                )[:1]

            if original_order:
                partner = original_order.partner_id
            elif username:
                partner = self.env['res.partner'].sudo().search(
                    [('neto_username', '=', username)], limit=1
                )
                if not partner:
                    RmaLog.create({
                        **base_vals,
                        'state': 'skipped',
                        'skip_reason': f'Partner not found for username {username}',
                    })
                    return
            else:
                RmaLog.create({
                    **base_vals,
                    'state': 'skipped',
                    'skip_reason': 'No order or username to identify partner',
                })
                return

            credit_note, note_lines = self._create_credit_note(
                rma_data, partner, store, original_invoice=original_invoice
            )

            msg_parts = []
            if original_order:
                msg_parts.append(Markup(
                    '<p>&#128279; <strong>Linked to Neto order:</strong> {oid}</p>'
                ).format(oid=lookup_id))
            if note_lines:
                items = Markup('').join(
                    Markup('<li>{n}</li>').format(n=n) for n in note_lines
                )
                msg_parts.append(Markup(
                    '<p>&#8505;&#65039; <strong>Neto RMA notes:</strong></p>'
                    '<ul>{items}</ul>'
                ).format(items=items))
            if msg_parts:
                credit_note.sudo().message_post(body=Markup('').join(msg_parts))

            RmaLog.create({
                **base_vals,
                'state':          'success',
                'credit_note_id': credit_note.id,
                'partner_id':     partner.id,
            })
            _logger.info('Neto RMA sync: RMA %s → %s', rma_id, credit_note.name)

        except Exception as exc:
            _logger.exception('Neto RMA sync: unhandled error on RMA %s', rma_id)
            RmaLog.create({**base_vals, 'state': 'error', 'error_message': str(exc)})

    def _sync_single_order_by_id(self, store, order_id):
        url = f"{store.store_url.rstrip('/')}/do/WS/NetoAPI"
        headers = {
            'Content-Type': 'application/json',
            'NETOAPI_ACTION': 'GetOrder',
            'NETOAPI_KEY': store.api_key,
            'Accept': 'application/json',
        }
        payload = {
            'Filter': {
                'OrderID': [order_id],
                'OutputSelector': GET_ORDER_OUTPUT_SELECTOR,
            }
        }
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=60)
            response.raise_for_status()
            body = response.json()
        except Exception as exc:
            _logger.warning('Neto RMA sync: GetOrder failed for %s — %s', order_id, exc)
            return
        orders = body.get('Order', [])
        if isinstance(orders, dict):
            orders = [orders]
        orders = orders or []
        if not orders:
            return
        synced_ids = set()
        synced_customers = set()
        self._process_order(orders[0], store, synced_ids, synced_customers)
        self.env.cr.commit()

    def _sync_rmas(self, store, since_dt, until_dt=None):
        self = self.with_context(mail_notrack=True, mail_create_nosubscribe=True, tracking_disable=True)
        _logger.info('Neto RMA sync [%s]: starting from %s', store.name, since_dt)
        try:
            rmas = self._fetch_rmas(store, since_dt, until_dt=until_dt)
        except Exception as exc:
            _logger.error('Neto RMA sync [%s]: _fetch_rmas failed — %s', store.name, exc)
            return
        synced_rma_ids = set()
        for rma_data in rmas:
            self._process_rma(rma_data, store, synced_rma_ids)
            self.env.cr.commit()
        store.sudo().write({'last_rma_sync_date': fields.Datetime.now()})
        _logger.info('Neto RMA sync [%s]: completed — %d RMA(s)', store.name, len(rmas))
