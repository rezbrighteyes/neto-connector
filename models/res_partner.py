# -*- coding: utf-8 -*-
from odoo import api, fields, models


class ResPartner(models.Model):
    _inherit = 'res.partner'

    neto_username = fields.Char(string='Neto Username', index=True)
    neto_account_balance = fields.Char(string='Neto Account Balance', readonly=True)
    neto_available_credit = fields.Char(string='Neto Available Credit', readonly=True)
    neto_on_credit_hold = fields.Boolean(string='On Credit Hold (Neto)', default=False)
    # Deliberately a Selection, not a Boolean. A Boolean cannot tell "Neto says
    # this customer is switched off" from "we have never asked Neto", and both
    # would read as False -- the exact ambiguity that has repeatedly caused this
    # data to be mis-read. Blank means unknown; check neto_last_sync.
    #
    # Neto is being decommissioned, so this is a RECORD of what Neto knew rather
    # than a live mirror: once the connector stops running, the value freezes at
    # whatever Neto last said. That is the point -- it keeps the answer inside
    # Odoo after the system that held it is gone.
    #
    # INFORMATIONAL ONLY. This must never be mirrored onto res.partner.active:
    # doing so is what archived 339 partners (one of them a trading store with
    # 347 orders) before 19.0.1.7.7 removed it. Archiving is a separate,
    # deliberate, audited decision -- see scripts/{audit,apply}_archive_neto_
    # inactive_partners.py.
    neto_status = fields.Selection(
        [('active', 'Active'), ('inactive', 'Inactive')],
        string='Neto Status',
        readonly=True,
        copy=False,
        help="Whether Neto reported this customer as active, as at Neto Last "
             "Sync. Blank means Neto was never asked about them. This does NOT "
             "archive the contact in Odoo and never affects ordering.",
    )
    neto_classification = fields.Char(string='Neto Classification')
    neto_last_sync = fields.Datetime(string='Neto Last Sync', readonly=True)
    neto_payment_count = fields.Integer(
        string='Neto Payments',
        compute='_compute_neto_payment_count',
    )

    def _get_neto_payment_company_domain(self):
        company_ids = self.env.companies.ids or [self.env.company.id]
        return [('company_id', 'in', company_ids)]

    @api.depends('child_ids')
    @api.depends_context('allowed_company_ids')
    def _compute_neto_payment_count(self):
        Payment = self.env['neto.payment'].sudo()
        for partner in self:
            commercial_partner = partner.commercial_partner_id or partner
            partner_ids = self.search([
                ('id', 'child_of', commercial_partner.id),
            ]).ids
            partner.neto_payment_count = Payment.search_count(
                [('partner_id', 'in', partner_ids)]
                + self._get_neto_payment_company_domain()
            )

    def action_view_neto_payments(self):
        self.ensure_one()
        commercial_partner = self.commercial_partner_id or self
        partner_ids = self.search([
            ('id', 'child_of', commercial_partner.id),
        ]).ids
        action = self.env['ir.actions.act_window']._for_xml_id(
            'Reza_neto_connector.neto_payment_action'
        )
        action['domain'] = (
            [('partner_id', 'in', partner_ids)]
            + self._get_neto_payment_company_domain()
        )
        action['context'] = {'default_partner_id': self.id}
        action['name'] = 'Neto Payments'
        return action
