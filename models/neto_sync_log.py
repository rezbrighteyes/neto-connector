# -*- coding: utf-8 -*-
from odoo import models, fields


class NetoSyncLog(models.Model):
    _name = 'neto.sync.log'
    _description = 'Neto Sync Log'
    _order = 'sync_date desc'

    neto_order_id = fields.Char(string='Neto Order ID', required=True, index=True)
    neto_username = fields.Char(string='Neto Username')
    neto_order_date = fields.Datetime(string='Order Date')
    neto_grand_total = fields.Float(string='Grand Total', digits=(16, 2))
    neto_order_status = fields.Char(string='Neto Status')
    state = fields.Selection(
        selection=[
            ('success', 'Success'),
            ('skipped', 'Skipped'),
            ('error', 'Error'),
        ],
        string='State',
        required=True,
        index=True,
    )
    skip_reason = fields.Char(string='Skip Reason')
    error_message = fields.Text(string='Error Message')
    missing_skus = fields.Text(string='Missing SKUs')
    store_id = fields.Many2one('neto.store', string='Store', ondelete='set null', index=True)
    sale_order_id = fields.Many2one('sale.order', string='Sale Order', ondelete='set null')
    partner_id = fields.Many2one('res.partner', string='Partner', ondelete='set null')
    partner_created = fields.Boolean(string='New Partner Created')
    sync_date = fields.Datetime(string='Sync Date', default=fields.Datetime.now, readonly=True)
    line_count = fields.Integer(string='Lines')

    def action_repair_missing_sku_lines(self):
        result = self.env['neto.connector'].sudo().repair_missing_sku_lines(self)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Neto Missing SKUs',
                'message': (
                    '%(lines_added)s line(s) added across %(orders_repaired)s order(s). '
                    '%(orders_still_missing)s order(s) still have missing SKUs.'
                ) % result,
                'type': 'success' if not result['orders_still_missing'] else 'warning',
                'sticky': False,
            },
        }
