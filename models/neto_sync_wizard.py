# -*- coding: utf-8 -*-
import logging
import requests
from datetime import datetime, timezone
from odoo import models, fields, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Must stay in sync with the OutputSelector in neto_connector.py
_OUTPUT_SELECTOR = [
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
]


class NetoSyncWizard(models.TransientModel):
    _name = 'neto.sync.wizard'
    _description = 'Sync Neto Orders'

    store_id = fields.Many2one(
        'neto.store', string='Store', required=True,
        domain=[('active', '=', True)],
        help='Which Neto store to fetch the order from.',
    )
    # --- Single order mode ---
    order_id_input = fields.Char(
        string='Neto Order ID',
        help='The Neto order number, e.g. GLE39259 or LIA36217. Leave blank to use date range.',
    )
    force_resync = fields.Boolean(
        string='Force Re-sync',
        default=False,
        help='If the order already exists in Odoo, patch its Neto fields '
             '(Date Paid, Payment Method, Delivery Address) from the latest API data '
             'without creating a duplicate.',
    )
    # --- Date range mode ---
    date_from = fields.Datetime(
        string='Date From',
        help='Sync orders updated from this date/time (UTC). Used when no Order ID is provided.',
    )
    date_to = fields.Datetime(
        string='Date To',
        help='Sync orders updated up to this date/time (UTC). Leave blank to sync up to now.',
    )
    result_message = fields.Text(string='Result', readonly=True)

    @api.onchange('order_id_input')
    def _onchange_order_id_input(self):
        """Clear date fields if a specific order ID is entered."""
        if self.order_id_input:
            self.date_from = False
            self.date_to = False

    def action_sync_order(self):
        self.ensure_one()
        store = self.store_id
        order_id = (self.order_id_input or '').strip().upper()

        if order_id:
            return self._sync_single_order(store, order_id)
        elif self.date_from:
            return self._sync_date_range(store, self.date_from, self.date_to)
        else:
            raise UserError(_('Please enter a Neto Order ID or set a Date From for date range sync.'))

    def _fetch_raw_order(self, store, order_id):
        """Call Neto API for a single order by ID. Returns the raw order dict or raises UserError."""
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
                'OutputSelector': _OUTPUT_SELECTOR,
            }
        }
        _logger.info('Neto single-order sync [%s]: fetching order %s', store.name, order_id)
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=60)
            response.raise_for_status()
            body = response.json()
        except Exception as exc:
            raise UserError(_('Neto API error: %s') % str(exc))

        if 'GetOrderResponse' in body:
            orders = body['GetOrderResponse'].get('Order', [])
        else:
            orders = body.get('Order', [])

        if isinstance(orders, dict):
            orders = [orders]
        orders = orders or []

        if not orders:
            raise UserError(
                _('Order %s not found in Neto store "%s".') % (order_id, store.name)
            )
        return orders[0]

    def _patch_existing_order(self, sale_order, order_data):
        """Patch Neto-sourced fields on an existing SO without touching order lines.

        Updates: neto_date_paid, neto_payment_method, partner_shipping_id.
        Safe to run on confirmed/locked orders.
        """
        connector = self.env['neto.connector']

        # DatePaid
        date_paid = connector._parse_neto_datetime(order_data.get('DatePaid'))

        # Payment method
        payments = order_data.get('OrderPayment', []) or []
        if isinstance(payments, dict):
            payments = [payments]
        payment_method = (payments[0].get('PaymentType') or '').strip() if payments else ''

        # Shipping address
        ship_partner = connector._get_or_create_ship_address(
            sale_order.partner_id, order_data
        )

        write_vals = {}
        if date_paid and 'neto_date_paid' in self.env['sale.order']._fields:
            write_vals['neto_date_paid'] = date_paid
        if payment_method and 'neto_payment_method' in self.env['sale.order']._fields:
            write_vals['neto_payment_method'] = payment_method
        if ship_partner:
            write_vals['partner_shipping_id'] = ship_partner.id

        if write_vals:
            sale_order.sudo().write(write_vals)
            _logger.info(
                'Neto re-sync: patched order %s — fields updated: %s',
                sale_order.name, list(write_vals.keys()),
            )
        return write_vals

    def _sync_single_order(self, store, order_id):
        existing = self.env['sale.order'].sudo().search(
            [('neto_order_id', '=', order_id)], limit=1
        )

        if existing and not self.force_resync:
            raise UserError(
                _('Order %s has already been synced — see %s.\n\n'
                  'Tick "Force Re-sync" to patch its Neto fields (Date Paid, '
                  'Payment Method, Delivery Address) from the latest API data.')
                % (order_id, existing.name)
            )

        order_data = self._fetch_raw_order(store, order_id)

        if existing and self.force_resync:
            # Patch only — don't create a duplicate
            patched = self._patch_existing_order(existing, order_data)
            self.env.cr.commit()
            return {
                'type': 'ir.actions.act_window',
                'res_model': 'sale.order',
                'res_id': existing.id,
                'view_mode': 'form',
                'target': 'current',
            }

        # New order — full sync
        connector = self.env['neto.connector']
        synced_ids = set()
        connector._process_order(order_data, store, synced_ids)
        self.env.cr.commit()

        sale_order = self.env['sale.order'].sudo().search(
            [('neto_order_id', '=', order_id)], limit=1
        )

        if sale_order:
            _logger.info(
                'Neto single-order sync: created %s for Neto order %s',
                sale_order.name, order_id,
            )
            return {
                'type': 'ir.actions.act_window',
                'res_model': 'sale.order',
                'res_id': sale_order.id,
                'view_mode': 'form',
                'target': 'current',
            }
        else:
            return {
                'type': 'ir.actions.act_window',
                'res_model': 'neto.sync.log',
                'view_mode': 'list,form',
                'domain': [('neto_order_id', '=', order_id)],
                'target': 'current',
                'name': f'Sync Log: {order_id}',
            }

    def _sync_date_range(self, store, date_from, date_to):
        since_dt = date_from.replace(tzinfo=timezone.utc)
        until_dt = date_to.replace(tzinfo=timezone.utc) if date_to else None

        connector = self.env['neto.connector']
        connector._sync_store(store, since_dt=since_dt, until_dt=until_dt)

        label = f"{date_from.strftime('%d/%m/%Y %H:%M')}"
        if date_to:
            label += f" → {date_to.strftime('%d/%m/%Y %H:%M')}"
        else:
            label += ' → now'

        return {
            'type': 'ir.actions.act_window',
            'res_model': 'neto.sync.log',
            'view_mode': 'list,form',
            'domain': [('store_id', '=', store.id)],
            'target': 'current',
            'name': f'Sync Log: {label}',
        }
