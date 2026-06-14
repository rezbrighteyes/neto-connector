# -*- coding: utf-8 -*-
import logging
import requests
from datetime import datetime, timedelta, timezone
from odoo import models, fields, api, _
from odoo.exceptions import UserError
from .neto_connector import GET_ORDER_OUTPUT_SELECTOR

_logger = logging.getLogger(__name__)


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
    import_as_history = fields.Boolean(
        string='Import As History (No Accounting)',
        default=False,
        help='If enabled, imported Neto orders stay as quotations/drafts and RMAs are '
             'saved as informational RMA Log records only. No invoices, credit notes, '
             'or refund payments are created.',
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
    queue_orders = fields.Boolean(string='Import Orders', default=True)
    queue_refresh_existing_orders = fields.Boolean(
        string='Refresh Existing Order Statuses',
        default=False,
        help='Re-fetch existing Neto orders by exact Order ID and refresh their Neto status, '
             'tracking, payment metadata, and delivery address without creating duplicates '
             'or changing accounting.',
    )
    queue_payments = fields.Boolean(string='Import Payments', default=True)
    queue_rmas = fields.Boolean(string='Import RMAs', default=False)
    chunk_days = fields.Integer(
        string='Days Per Background Chunk',
        default=7,
        help='Creates one background job per chunk so large history imports do not depend on the browser staying open.',
    )

    @api.onchange('order_id_input')
    def _onchange_order_id_input(self):
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

    def action_queue_history_import(self):
        self.ensure_one()
        if not self.date_from or not self.date_to:
            raise UserError(_('Set both Date From and Date To before queueing a history import.'))
        if self.date_to <= self.date_from:
            raise UserError(_('Date To must be after Date From.'))
        if (
            not self.queue_orders
            and not self.queue_refresh_existing_orders
            and not self.queue_payments
            and not self.queue_rmas
        ):
            raise UserError(_(
                'Select at least one import type: orders, existing-order refresh, payments, or RMAs.'
            ))

        chunk_days = max(self.chunk_days or 1, 1)
        Job = self.env['neto.history.import.job'].sudo()
        created = Job
        chunk_start = self.date_from
        while chunk_start < self.date_to:
            chunk_end = min(chunk_start + timedelta(days=chunk_days), self.date_to)
            created |= Job.create({
                'name': '%s %s -> %s' % (
                    self.store_id.name,
                    chunk_start.strftime('%Y-%m-%d'),
                    chunk_end.strftime('%Y-%m-%d'),
                ),
                'store_id': self.store_id.id,
                'date_from': chunk_start,
                'date_to': chunk_end,
                'import_orders': self.queue_orders,
                'refresh_existing_orders': self.queue_refresh_existing_orders,
                'import_payments': self.queue_payments,
                'import_rmas': self.queue_rmas,
                'import_as_history': self.import_as_history,
            })
            chunk_start = chunk_end

        return {
            'type': 'ir.actions.act_window',
            'res_model': 'neto.history.import.job',
            'view_mode': 'list,form',
            'domain': [('id', 'in', created.ids)],
            'target': 'current',
            'name': _('Queued Neto History Import Jobs'),
        }

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
                'OutputSelector': GET_ORDER_OUTPUT_SELECTOR,
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

    def _patch_existing_order(self, sale_order, order_data, store):
        """Patch Neto-sourced fields on an existing SO without touching order lines."""
        connector = self.env['neto.connector']
        return connector._patch_existing_order_from_neto(sale_order, order_data, store)

    def _sync_single_order(self, store, order_id):
        existing = self.env['sale.order'].sudo().search(
            [
                ('neto_order_id', '=', order_id),
                ('company_id', '=', store.company_id.id),
            ],
            limit=1,
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
            self._patch_existing_order(existing, order_data, store)
            self.env.cr.commit()
            return {
                'type': 'ir.actions.act_window',
                'res_model': 'sale.order',
                'res_id': existing.id,
                'view_mode': 'form',
                'target': 'current',
            }

        connector = self.env['neto.connector']
        synced_ids = set()
        connector._process_order(
            order_data, store, synced_ids,
            import_as_history=self.import_as_history,
        )
        self.env.cr.commit()

        sale_order = self.env['sale.order'].sudo().search(
            [
                ('neto_order_id', '=', order_id),
                ('company_id', '=', store.company_id.id),
            ],
            limit=1,
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
        connector._sync_store(
            store, since_dt=since_dt, until_dt=until_dt,
            import_as_history=self.import_as_history,
        )

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
