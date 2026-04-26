# -*- coding: utf-8 -*-
from odoo import models, fields


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    neto_order_id = fields.Char(string='Neto Order ID', index=True, copy=False)
