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
    store_id = fields.Many2one('neto.store', string='Store', ondelete='set null', index=True)
    sale_order_id = fields.Many2one('sale.order', string='Sale Order', ondelete='set null')
    partner_id = fields.Many2one('res.partner', string='Partner', ondelete='set null')
    partner_created = fields.Boolean(string='New Partner Created')
    sync_date = fields.Datetime(string='Sync Date', default=fields.Datetime.now, readonly=True)
    line_count = fields.Integer(string='Lines')
