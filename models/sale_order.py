# -*- coding: utf-8 -*-
from odoo import models, fields


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    neto_order_id = fields.Char(string='Neto Order ID', index=True, copy=False)
    neto_order_status = fields.Char(string='Neto Status', copy=False, readonly=True)
    neto_date_paid = fields.Datetime(string='Neto Date Paid', copy=False, readonly=True)
    neto_payment_method = fields.Char(string='Neto Payment Method', copy=False, readonly=True)
