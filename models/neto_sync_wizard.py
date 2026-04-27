# -*- coding: utf-8 -*-
import logging
import requests
from odoo import models, fields, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

_OUTPUT_SELECTOR = [
    'OrderID', 'Username', 'Email', 'GrandTotal',
    'ShippingTotal', 'SurchargeTotal', 'OrderStatus',
    'OrderLine', 'OrderLine.SKU', 'OrderLine.ProductName',
    'OrderLine.UnitPrice', 'OrderLine.Quantity',
    'BillAddress', 'DatePlaced', 'DateUpdated',
]


class NetoSyncWizard(models.TransientModel):
    _name = 'neto.sync.wizard'
    _description = 'Sync a Single Neto Order'

    store_id = fields.Many2one(
        'neto.store', string='Store', required=True,
        domain=[('active', '=', True)],
        help='Which Neto store to fetch the order from.',
    )
    order_id_input = fields.Char(
        string='Neto Order ID',
        required=True,
        help='The Neto order number, e.g. GLE39259 or LIA00001234.',
    )
    result_message = fields.Text(string='Result', readonly=True)

    def action_sync_order(self):
        self.ensure_one()
        store = self.store_id
        order_id = (self.order_id_input or '').strip().upper()

        if not order_id:
            raise UserError(_('Please enter a Neto Order ID.'))

        # Check if already synced
        existing = self.env['sale.order'].sudo().search(
            [('neto_order_id', '=', order_id)], limit=1
        )
        if existing:
            raise UserError(
                _('Order %s has already been synced — see %s.') % (order_id, existing.name)
            )

        # Fetch from Neto API
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

        _logger.info(
            'Neto single-order sync [%s]: fetching order %s', store.name, order_id
        )
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

        # Find the created order to open it
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
            # Synced but skipped (status filter, zero value, etc.) — show log
            return {
                'type': 'ir.actions.act_window',
                'res_model': 'neto.sync.log',
                'view_mode': 'list,form',
                'domain': [('neto_order_id', '=', order_id)],
                'target': 'current',
                'name': f'Sync Log: {order_id}',
            }
