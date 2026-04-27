# -*- coding: utf-8 -*-
from odoo import models, fields


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    neto_order_id = fields.Char(string='Neto Order ID', index=True, copy=False)
    neto_order_status = fields.Char(string='Neto Status', copy=False, readonly=True)
