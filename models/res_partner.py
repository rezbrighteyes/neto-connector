# -*- coding: utf-8 -*-
from odoo import api, fields, models


class ResPartner(models.Model):
    _inherit = 'res.partner'

    neto_username = fields.Char(string='Neto Username', index=True)
    neto_account_balance = fields.Char(string='Neto Account Balance', readonly=True)
    neto_available_credit = fields.Char(string='Neto Available Credit', readonly=True)
    neto_on_credit_hold = fields.Boolean(string='On Credit Hold (Neto)', default=False)
    neto_classification = fields.Char(string='Neto Classification')
    neto_last_sync = fields.Datetime(string='Neto Last Sync', readonly=True)
    neto_payment_count = fields.Integer(
        string='Neto Payments',
        compute='_compute_neto_payment_count',
    )

    @api.depends('child_ids')
    def _compute_neto_payment_count(self):
        Payment = self.env['neto.payment'].sudo()
        for partner in self:
            commercial_partner = partner.commercial_partner_id or partner
            partner_ids = self.search([
                ('id', 'child_of', commercial_partner.id),
            ]).ids
            partner.neto_payment_count = Payment.search_count([
                ('partner_id', 'in', partner_ids),
            ])

    def action_view_neto_payments(self):
        self.ensure_one()
        commercial_partner = self.commercial_partner_id or self
        partner_ids = self.search([
            ('id', 'child_of', commercial_partner.id),
        ]).ids
        action = self.env['ir.actions.act_window']._for_xml_id(
            'Reza_neto_connector.neto_payment_action'
        )
        action['domain'] = [('partner_id', 'in', partner_ids)]
        action['context'] = {'default_partner_id': self.id}
        action['name'] = 'Neto Payments'
        return action
