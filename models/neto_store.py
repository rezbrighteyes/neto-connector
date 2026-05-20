# -*- coding: utf-8 -*-
from odoo import models, fields


class NetoStore(models.Model):
    _name = 'neto.store'
    _description = 'Neto Store'
    _order = 'name'

    name = fields.Char(string='Store Name', required=True)
    store_url = fields.Char(string='Store URL', required=True)
    api_key = fields.Char(string='API Key', required=True, password=True)
    active = fields.Boolean(string='Active', default=True)
    last_sync_date = fields.Datetime(string='Last Sync', readonly=True)
    last_rma_sync_date = fields.Datetime(string='Last RMA Sync', readonly=True)
    last_product_sync_date = fields.Datetime(string='Last Product Sync', readonly=True)
    company_id = fields.Many2one(
        'res.company', string='Company', required=True,
        default=lambda self: self.env.company,
    )
    warehouse_id = fields.Many2one(
        'stock.warehouse', string='Warehouse', required=True,
        domain="[('company_id', '=', company_id)]",
    )
    neto_default_categ_id = fields.Many2one(
        'product.category',
        string='Default Product Category',
        help='Fallback category used when Neto returns no usable category data.',
    )
