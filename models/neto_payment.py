# -*- coding: utf-8 -*-
from odoo import api, fields, models


class NetoPayment(models.Model):
    _name = 'neto.payment'
    _description = 'Neto Payment'
    _order = 'date_paid desc, neto_payment_id desc'
    _sql_constraints = [
        (
            'neto_payment_id_uniq',
            'unique(neto_payment_id)',
            'This Neto payment has already been synced.',
        ),
    ]

    neto_payment_id = fields.Char(string='Neto Payment ID', required=True, index=True, copy=False)
    neto_order_id = fields.Char(string='Neto Order ID', index=True, copy=False)
    sale_order_id = fields.Many2one('sale.order', string='Sale Order', index=True, ondelete='set null')
    partner_id = fields.Many2one('res.partner', string='Customer', index=True, ondelete='set null')
    store_id = fields.Many2one('neto.store', string='Neto Store', required=True, index=True, ondelete='restrict')
    company_id = fields.Many2one('res.company', string='Company', required=True, index=True, ondelete='restrict')
    amount_paid = fields.Monetary(string='Amount Paid', currency_field='currency_id')
    currency_id = fields.Many2one(
        'res.currency',
        string='Currency',
        required=True,
        default=lambda self: self._default_aud_currency(),
        ondelete='restrict',
    )
    currency_code = fields.Char(string='Neto Currency Code', copy=False)
    date_paid = fields.Datetime(string='Date Paid', index=True, copy=False)
    payment_method = fields.Char(string='Payment Method', copy=False)
    payment_method_name = fields.Char(string='Payment Method Name', copy=False)
    process_by = fields.Char(string='Process By', copy=False)
    payment_notes = fields.Text(string='Payment Notes', copy=False)
    days_to_pay = fields.Integer(
        string='Days to Pay',
        compute='_compute_payment_timing',
        store=True,
        readonly=True,
    )
    is_late = fields.Boolean(
        string='Late',
        compute='_compute_payment_timing',
        store=True,
        readonly=True,
    )
    is_orphan_payment = fields.Boolean(
        string='Orphan Payment',
        default=True,
        index=True,
        copy=False,
    )

    @api.model
    def _default_aud_currency(self):
        return (
            self.env['res.currency'].sudo().search([('name', '=', 'AUD')], limit=1)
            or self.env.company.currency_id
        )

    @api.depends('date_paid', 'sale_order_id.date_order')
    def _compute_payment_timing(self):
        for payment in self:
            if payment.date_paid and payment.sale_order_id.date_order:
                date_paid = fields.Datetime.to_datetime(payment.date_paid)
                date_order = fields.Datetime.to_datetime(payment.sale_order_id.date_order)
                payment.days_to_pay = max((date_paid.date() - date_order.date()).days, 0)
                payment.is_late = payment.days_to_pay > 0
            else:
                payment.days_to_pay = False
                payment.is_late = False
