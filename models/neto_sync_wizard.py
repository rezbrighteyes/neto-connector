# -*- coding: utf-8 -*-
import logging
import requests
from datetime import datetime, timezone
from odoo import models, fields, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

_OUTPUT_SELECTOR = [
    'OrderID', 'Username', 'Email', 'GrandTotal',
    'ShippingTotal', 'SurchargeTotal', 'OrderStatus',
    'CouponCode', 'CouponDiscount',
    'OrderLine', 'OrderLine.SKU', 'OrderLine.ProductName',
    'OrderLine.UnitPrice', 'OrderLine.Quantity',
    'OrderLine.PercentDiscount', 'OrderLine.ProductDiscount',
    'OrderLine.CouponDiscount',
    'BillAddress', 'BillingEmail', 'BillingName', 'BillingAddress',
    'DatePlaced', 'DateUpdated',
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
        help='The Neto order number, e.g. GLE39259 or LIA00001234. Leave blank to use date range.',
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

    def _sync_single_order(self, store, order_id):
        # Check if already synced
        existing = self.env['sale.order'].sudo().search(
            [('neto_order_id', '=', order_id)], limit=1
        )
        if existing:
            raise UserError(
                _('Order %s has already been synced — see %s.') % (order_id, existing.name)
            )

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

        connector = self.env['neto.connector']
        synced_ids = set()
        connector._process_order(orders[0], store, synced_ids)
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
        # Convert naive Odoo datetimes to UTC-aware
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
