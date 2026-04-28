# -*- coding: utf-8 -*-
import logging
import requests
from datetime import datetime, timedelta, timezone

from markupsafe import Markup
from odoo import models, fields

_logger = logging.getLogger(__name__)

_API_ACTION = 'GetOrder'
_GST_DIVISOR = 1.1  # Neto UnitPrice is GST-inclusive; divide to get ex-GST for Odoo
_SURCHARGE_SKU = 'NETO-SURCHARGE'  # internal product SKU for surcharge lines
_SHIPPING_SKU  = 'NETO_SHIPPING'   # internal product SKU for shipping lines

# Neto internal line-type prefixes / exact SKUs.
#   TEXT_NOTE  — free-text note lines: collected and shown as FYI in chatter
#   DS_*       — drop-ship instruction lines: silently dropped
_SKIP_SKU_PREFIXES = ('DS_',)
_NOTE_SKU_EXACT    = frozenset({'TEXT_NOTE'})

# Suffix appended to auto-created product names so they are easy to spot
_NETO_UNSYNCED_SUFFIX = '[NETO-UNSYNCED]'

# Internal email domain — orders from this domain are synced but flagged
_INTERNAL_EMAIL_DOMAIN = '@brighteyes.net.au'

# Neto statuses that should cancel the Odoo order
_CANCEL_STATUSES = frozenset({'Cancelled', 'Declined'})

# Neto statuses that are dispatched (confirmed + flagged via neto_order_status)
# NOTE: Odoo 19 removed the 'done' (Locked) state from sale.order entirely.
# Dispatched orders are confirmed ('sale') and identified by neto_order_status.
_DISPATCHED_STATUSES = frozenset({'Dispatched'})

# Only create invoices for orders placed within this many days.
# Historical orders get a sale.order only — no invoice created.
_INVOICE_CUTOFF_DAYS = 60

# GetItem OutputSelectors we need for product creation
_GETITEM_OUTPUT = [
    'Name', 'Brand', 'Model',
    'DefaultPrice', 'RRP', 'CostPrice',
    'TaxInclusive',
    'UPC', 'UPC1',
    'ShippingWeight',
    'IsActive',
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

    def _is_internal_sku(self, sku):
        """Return True for DS_* drop-ship lines that should be silently skipped."""
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

    def _fetch_payment_method(self, store, order_id, order_data=None):
        """Return the payment method string for an order.

        Strategy:
        1. Call GetPayment — works for gateway-paid orders (credit card, PayPal, etc.)
        2. If GetPayment returns no records (account/wholesale/EFT orders), fall back
           to the PaymentMethod field on the GetOrder response itself.

        Returns empty string on any error so the sync never fails because of this.
        """
        url = f"{store.store_url.rstrip('/')}/do/WS/NetoAPI"
        headers = {
            'Content-Type': 'application/json',
            'NETOAPI_ACTION': 'GetPayment',
            'NETOAPI_KEY': store.api_key,
            'Accept': 'application/json',
        }
        payload = {
            'Filter': {
                'OrderID': [order_id],
                'OutputSelector': [
                    'ID', 'PaymentMethod', 'PaymentMethodName',
                    'AmountPaid', 'DatePaid',
                ],
            }
        }
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=30)
            response.raise_for_status()
            body = response.json()
        except Exception as exc:
            _logger.warning(
                'Neto sync: GetPayment failed for order %s — %s', order_id, exc
            )
            return self._payment_method_from_order(order_data, order_id)

        _logger.info(
            'Neto sync: GetPayment raw response for order %s: %s',
            order_id, str(body)[:2000],
        )

        if 'GetPaymentResponse' in body:
            payments = body['GetPaymentResponse'].get('Payment', [])
        else:
            payments = body.get('Payment', [])

        if isinstance(payments, dict):
            payments = [payments]
        payments = payments or []

        if not payments:
            _logger.info(
                'Neto sync: GetPayment returned no records for order %s '
                '— falling back to GetOrder PaymentMethod field',
                order_id,
            )
            return self._payment_method_from_order(order_data, order_id)

        method = (payments[0].get('PaymentMethodName') or payments[0].get('PaymentMethod') or '').strip()
        _logger.info(
            'Neto sync: GetPayment order %s — PaymentMethodName=%r  PaymentMethod=%r',
            order_id,
            payments[0].get('PaymentMethodName'),
            payments[0].get('PaymentMethod'),
        )
        return method

    def _payment_method_from_order(self, order_data, order_id):
        """Extract PaymentMethod directly from the GetOrder response dict."""
        if not order_data:
            return ''
        method = (order_data.get('PaymentMethod') or '').strip()
        if method:
            _logger.info(
                'Neto sync: order %s PaymentMethod from GetOrder = %r', order_id, method
            )
        else:
            _logger.info(
                'Neto sync: order %s has no PaymentMethod in GetOrder response either',
                order_id,
            )
        return method

    def _get_or_create_ship_address(self, partner, order_data):
        """Return a child delivery address partner for this order's ShipAddress fields."""
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
    # Auto-create missing products via GetItem
    # -------------------------------------------------------------------------

    def _fetch_neto_item(self, store, sku):
        """Call Neto GetItem for a single SKU. Returns the item dict or None."""
        url = f"{store.store_url.rstrip('/')}/do/WS/NetoAPI"
        headers = {
            'Content-Type': 'application/json',
            'NETOAPI_ACTION': 'GetItem',
            'NETOAPI_KEY': store.api_key,
            'Accept': 'application/json',
        }
        payload = {
            'Filter': {
                'SKU': [sku],
                'OutputSelector': _GETITEM_OUTPUT,
            }
        }
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=30)
            response.raise_for_status()
            body = response.json()
        except Exception as exc:
            _logger.warning('Neto sync: GetItem failed for SKU %s — %s', sku, exc)
            return None

        if 'GetItemResponse' in body:
            items = body['GetItemResponse'].get('Item', [])
        else:
            items = body.get('Item', [])

        if isinstance(items, dict):
            items = [items]
        items = items or []

        if not items:
            _logger.warning('Neto sync: GetItem returned no data for SKU %s', sku)
            return None

        return items[0]

    def _get_or_create_product_from_neto(self, store, sku, line_data):
        """Look up SKU in Odoo; if missing, call GetItem and create a placeholder product.

        Returns (product, was_created).
        """
        Product = self.env['product.product'].sudo()

        product = Product.search([('default_code', '=', sku)], limit=1)
        if product:
            return product, False

        item = self._fetch_neto_item(store, sku)
        if item:
            neto_barcode = (item.get('UPC') or item.get('UPC1') or '').strip()
            if neto_barcode:
                product = Product.search([('barcode', '=', neto_barcode)], limit=1)
                if product:
                    _logger.info(
                        'Neto sync: SKU=%s matched existing product "%s" via barcode %s',
                        sku, product.name, neto_barcode,
                    )
                    return product, False

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
        if item:
            barcode = (item.get('UPC') or item.get('UPC1') or '').strip() or None

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
        }
        if barcode:
            vals['barcode'] = barcode
        if weight:
            vals['weight'] = weight

        try:
            product = Product.create(vals)
            _logger.info(
                'Neto sync: auto-created product "%s" (SKU=%s, price=%.4f, barcode=%s)',
                product_name, sku, list_price, barcode or 'none',
            )
            return product, True
        except Exception as exc:
            _logger.warning(
                'Neto sync: could not auto-create product SKU=%s — %s', sku, exc
            )
            return None, False

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
                    'OrderID', 'Username', 'Email',
                    'BillAddress',
                    'ShipAddress',
                    'GrandTotal', 'SurchargeTotal', 'ShippingTotal',
                    'OrderStatus',
                    'PaymentMethod',
                    'OrderLine', 'OrderLine.SKU',
                    'OrderLine.ProductName', 'OrderLine.UnitPrice',
                    'OrderLine.Quantity', 'OrderLine.PercentDiscount',
                    'OrderLine.ProductDiscount',
                    'DatePlaced', 'DateUpdated',
                    'DatePaid',
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
    # Customer sync
    # -------------------------------------------------------------------------

    def _sync_customer(self, store, partner, username):
        """Pull customer credit/account data from Neto and write it to the Odoo partner.

        Called after partner is found or created. Never raises — logs WARNING and returns.
        """
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
            term = self.env['account.payment.term'].sudo().search(
                [('name', 'ilike', invoice_terms)], limit=1
            )
            if term:
                vals['property_payment_term_id'] = term.id
            else:
                _logger.warning(
                    'Neto sync: payment term "%s" not found for username %s — leaving unchanged',
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

        try:
            partner.sudo().write(vals)
            _logger.info('Neto sync: GetCustomer synced for username %s', username)
        except Exception as exc:
            _logger.warning(
                'Neto sync: could not write customer fields for username %s — %s',
                username, exc,
            )

    # -------------------------------------------------------------------------
    # Partner
    # -------------------------------------------------------------------------

    def _get_or_create_partner(self, username, order_data, store, synced_customers=None):
        if synced_customers is None:
            synced_customers = set()
        """Find partner by neto_username or create one from the order's billing fields."""
        Partner = self.env['res.partner'].sudo()
        partner = Partner.search([('neto_username', '=', username)], limit=1)
        if partner:
            if username not in synced_customers:
                self._sync_customer(store, partner, username)
                synced_customers.add(username)
            return partner, False

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
        self._sync_customer(store, partner, username)
        synced_customers.add(username)
        return partner, True

    # -------------------------------------------------------------------------
    # Invoice creation
    # -------------------------------------------------------------------------

    def _get_payment_journal(self, payment_method):
        """Return an account.journal for the given payment method name.

        Searches bank/cash journals by name ilike payment_method.
        Falls back to the first available bank/cash journal if no match.
        """
        Journal = self.env['account.journal'].sudo()
        if payment_method:
            journal = Journal.search(
                [('type', 'in', ('bank', 'cash')), ('name', 'ilike', payment_method)],
                limit=1,
            )
            if journal:
                _logger.info(
                    'Neto sync: payment journal "%s" selected for method "%s"',
                    journal.name, payment_method,
                )
                return journal
            _logger.warning(
                'Neto sync: no journal matching "%s" — using first available bank/cash journal',
                payment_method,
            )
        journal = Journal.search([('type', 'in', ('bank', 'cash'))], limit=1)
        if journal:
            _logger.info(
                'Neto sync: fallback payment journal "%s" selected', journal.name
            )
        return journal

    def _create_invoice(self, order, payment_method, date_paid):
        """Create, post, and optionally pay an invoice for a confirmed sale order.

        Never raises — logs WARNING with order ID on any failure.
        """
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
                journal = self._get_payment_journal(payment_method)
                if not journal:
                    _logger.warning(
                        'Neto sync: no payment journal found for order %s — skipping payment registration',
                        order.neto_order_id,
                    )
                    return

                payment = self.env['account.payment'].sudo().create({
                    'payment_type': 'inbound',
                    'partner_type': 'customer',
                    'partner_id': order.partner_id.id,
                    'amount': invoice.amount_total,
                    'date': date_paid,
                    'journal_id': journal.id,
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
                    order.neto_order_id, invoice.amount_total, payment_method,
                )
        except Exception as exc:
            _logger.warning(
                'Neto sync: could not create invoice/payment for order %s — %s',
                order.neto_order_id, exc,
            )

    # -------------------------------------------------------------------------
    # Order creation / state management
    # -------------------------------------------------------------------------

    def _set_order_state(self, order, order_id, order_status, date_order, line_prices):
        """Set the final state of a synced sale order.

        Uses direct write() to avoid triggering stock.move creation.

        NOTE: Odoo 19 removed the 'done' (Locked) state from sale.order.
        Valid states are: draft, sent, sale, cancel.
        Dispatched orders are confirmed ('sale') and identified by
        the neto_order_status field on the record.

        State mapping:
          Cancelled / Declined  -> 'cancel'
          Everything else       -> 'sale'  (confirmed Sales Order)
        """
        if order_status in _CANCEL_STATUSES:
            target_state = 'cancel'
        else:
            target_state = 'sale'

        writes = {'state': target_state}
        if date_order:
            writes['date_order'] = date_order

        order.sudo().write(writes)

        # Restore Neto prices after state change (Odoo may reprice on confirm)
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

    def _create_sale_order(self, order_data, partner, store, neto_internal=False):
        Order = self.env['sale.order'].sudo()
        OrderLine = self.env['sale.order.line'].sudo()
        Product = self.env['product.product'].sudo()

        order_id = order_data.get('OrderID', '')

        date_order = (
            self._parse_neto_datetime(order_data.get('DatePlaced'))
            or fields.Datetime.now()
        )

        date_paid = self._parse_neto_datetime(order_data.get('DatePaid'))
        payment_method = self._fetch_payment_method(store, order_id, order_data=order_data)
        ship_partner = self._get_or_create_ship_address(partner, order_data)

        order_status = order_data.get('OrderStatus', '') or ''

        order_vals = {
            'partner_id':           partner.id,
            'partner_shipping_id':  ship_partner.id,
            'neto_order_id':        order_id,
            'neto_order_status':    order_status,
            'neto_internal':        neto_internal,
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

        line_prices = {}
        missing_lines = []
        autocreated_lines = []
        note_lines = []

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

            product = Product.search([('default_code', '=', sku)], limit=1)
            was_autocreated = False
            if not product:
                product, was_autocreated = self._get_or_create_product_from_neto(
                    store, sku, line
                )

            if not product:
                _logger.warning(
                    'Neto sync: SKU "%s" could not be found or created for order %s — skipping line',
                    sku, order_id,
                )
                missing_lines.append({
                    'sku': sku,
                    'name': line.get('ProductName') or '',
                    'qty': line.get('Quantity') or '',
                    'price': f"{float(line.get('UnitPrice') or 0):.2f}",
                })
                continue

            if was_autocreated:
                autocreated_lines.append({
                    'sku': sku,
                    'name': product.name,
                    'qty': line.get('Quantity') or '',
                    'price': f"{float(line.get('UnitPrice') or 0):.2f}",
                })

            neto_price_incl = float(line.get('UnitPrice') or 0)
            neto_price_excl = round(neto_price_incl / _GST_DIVISOR, 4)
            qty = float(line.get('Quantity') or 1)

            percent_discount = float(line.get('PercentDiscount') or 0)
            if not percent_discount:
                product_discount_amt = float(line.get('ProductDiscount') or 0)
                line_total_incl = neto_price_incl * qty
                if product_discount_amt and line_total_incl:
                    percent_discount = round(
                        product_discount_amt / line_total_incl * 100, 4
                    )

            _logger.debug(
                'Neto sync: order %s  SKU=%s  qty=%.0f  unit_incl=%.4f  '
                'unit_excl=%.4f  disc%%=%.4f',
                order_id, sku, qty, neto_price_incl, neto_price_excl, percent_discount,
            )

            try:
                OrderLine.create({
                    'order_id': order.id,
                    'product_id': product.id,
                    'product_uom_qty': qty,
                    'price_unit': neto_price_excl,
                    'discount': percent_discount,
                    'name': product.name,
                    'product_uom_id': product.uom_id.id,
                })
                line_prices[product.id] = (neto_price_excl, percent_discount)
            except Exception as line_exc:
                _logger.warning(
                    'Neto sync: could not create line SKU=%s on order %s — %s',
                    sku, order_id, line_exc,
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
                    surcharge_excl, order_id,
                )
            except Exception as sc_exc:
                _logger.warning(
                    'Neto sync: could not create surcharge line on order %s — %s',
                    order_id, sc_exc,
                )

        # --- Shipping line ---
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
                        shipping_excl, order_id,
                    )
                except Exception as sh_exc:
                    _logger.warning(
                        'Neto sync: could not create shipping line on order %s — %s',
                        order_id, sh_exc,
                    )

        # --- Set final order state (no stock moves generated) ---
        final_state = self._set_order_state(
            order, order_id, order_status, date_order, line_prices
        )

        # --- Auto-create invoice for confirmed orders (recent only) ---
        if final_state == 'sale':
            from datetime import timedelta
            cutoff = fields.Datetime.now() - timedelta(days=_INVOICE_CUTOFF_DAYS)
            if date_order and date_order >= cutoff:
                self._create_invoice(order, payment_method, date_paid)
            else:
                _logger.info(
                    'Neto sync: order %s is older than %d days — skipping invoice creation',
                    order_id, _INVOICE_CUTOFF_DAYS,
                )

        # -----------------------------------------------------------------------
        # Post chatter message
        # -----------------------------------------------------------------------
        msg_parts = []

        if neto_internal:
            msg_parts.append(Markup('<p>⚠️ No billable items.</p>'))

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
                ).format(
                    sku=m['sku'], name=m['name'], qty=m['qty'], price=m['price'],
                )
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
                '</tr></thead>'
                '<tbody>{rows}</tbody></table>'
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
                ).format(
                    sku=m['sku'], name=m['name'], qty=m['qty'], price=m['price'],
                )
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
                '</tr></thead>'
                '<tbody>{rows}</tbody></table>'
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

    # -------------------------------------------------------------------------
    # Per-order processing
    # -------------------------------------------------------------------------

    def _process_order(self, order_data, store, synced_ids, synced_customers=None):
        if synced_customers is None:
            synced_customers = set()
        """Process a single Neto order dict — ALL orders are synced."""
        SyncLog = self.env['neto.sync.log'].sudo()

        order_id = order_data.get('OrderID', '')
        username = order_data.get('Username', '') or ''
        billing_email = (order_data.get('Email') or '').strip()
        grand_total = float(order_data.get('GrandTotal') or 0)
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
            if self.env['sale.order'].sudo().search_count(
                [('neto_order_id', '=', order_id)]
            ):
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

            partner, partner_created = self._get_or_create_partner(username, order_data, store, synced_customers)
            sale_order, missing_lines = self._create_sale_order(
                order_data, partner, store, neto_internal=neto_internal
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
                'skip_reason':     reason,
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
        synced_customers = set()
        _logger.info('Neto sync [%s]: %d order(s) to process', store.name, len(orders))
        for order_data in orders:
            self._process_order(order_data, store, synced_ids, synced_customers)
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
