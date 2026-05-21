# -*- coding: utf-8 -*-
from odoo import fields, models


class NetoInvoiceTermsMap(models.Model):
    _name = 'neto.invoice.terms.map'
    _description = 'Neto Invoice Terms Map'
    _order = 'store_id, neto_invoice_terms, id'

    active = fields.Boolean(default=True)
    store_id = fields.Many2one(
        'neto.store',
        string='Store',
        help='Optional store scope. Leave blank to allow the row to apply to any store.',
    )
    neto_invoice_terms = fields.Char(
        string='Neto Invoice Terms',
        required=True,
        index=True,
        help='Exact DefaultInvoiceTerms value returned by Neto GetCustomer.',
    )
    payment_term_id = fields.Many2one(
        'account.payment.term',
        string='Odoo Payment Terms',
        required=True,
    )
    note = fields.Char(string='Notes')
