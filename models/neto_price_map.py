# -*- coding: utf-8 -*-
from odoo import api, fields, models
from odoo.exceptions import ValidationError


class NetoPriceMap(models.Model):
    _name = 'neto.price.map'
    _description = 'Neto Imported Price Map'
    _order = 'store_id, range_name, inventory_id, id'

    active = fields.Boolean(default=True)
    store_id = fields.Many2one(
        'neto.store',
        string='Store',
        help='Optional store scope. Leave blank to allow the row to apply to any store.',
    )
    company_id = fields.Many2one(
        'res.company',
        string='Company',
        compute='_compute_company_id',
        store=True,
    )
    pricelist_id = fields.Many2one(
        'product.pricelist',
        string='Sales Pricelist',
        domain="[('company_id', 'in', [company_id, False])]",
        help='Optional Odoo pricelist that should receive the imported fixed price for this row.',
    )
    file = fields.Char(string='Source File', index=True)
    sheet = fields.Char(string='Sheet')
    range_name = fields.Char(string='Range Name', index=True)
    inventory_id = fields.Char(string='Inventory ID', required=True, index=True)
    description = fields.Char(string='Description')
    unit_price = fields.Float(string='Unit Price', digits='Product Price')
    rrp = fields.Float(string='RRP', digits='Product Price')
    note = fields.Char(string='Notes')

    @fields.depends('store_id.company_id')
    def _compute_company_id(self):
        for record in self:
            record.company_id = record.store_id.company_id

    @api.constrains('store_id', 'pricelist_id', 'inventory_id', 'file', 'sheet', 'description')
    def _check_duplicate_rows(self):
        for record in self:
            inventory_id = (record.inventory_id or '').strip()
            file_name = (record.file or '').strip()
            sheet_name = (record.sheet or '').strip()
            description = (record.description or '').strip()
            duplicates = self.search([
                ('id', '!=', record.id),
                ('store_id', '=', record.store_id.id or False),
                ('pricelist_id', '=', record.pricelist_id.id or False),
                ('inventory_id', '=', inventory_id),
                ('file', '=', file_name),
                ('sheet', '=', sheet_name),
                ('description', '=', description),
            ], limit=1)
            if duplicates:
                raise ValidationError('This imported price map row already exists.')
