# -*- coding: utf-8 -*-
from odoo import api, fields, models
from odoo.exceptions import ValidationError


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
    company_id = fields.Many2one(
        'res.company',
        string='Company',
        compute='_compute_company_id',
        store=True,
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
        domain="[('company_id', 'in', [company_id, False])]",
    )
    note = fields.Char(string='Notes')

    @api.depends('store_id.company_id')
    def _compute_company_id(self):
        for record in self:
            record.company_id = record.store_id.company_id

    @api.constrains('store_id', 'neto_invoice_terms')
    def _check_duplicate_terms(self):
        for record in self:
            neto_terms = (record.neto_invoice_terms or '').strip()
            duplicate = self.search([
                ('id', '!=', record.id),
                ('store_id', '=', record.store_id.id or False),
                ('neto_invoice_terms', '=ilike', neto_terms),
            ], limit=1)
            if duplicate:
                raise ValidationError('This Neto invoice terms mapping already exists.')
