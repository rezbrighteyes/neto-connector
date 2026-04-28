# -*- coding: utf-8 -*-
from odoo import models, fields


class ResPartner(models.Model):
    _inherit = 'res.partner'

    neto_username = fields.Char(string='Neto Username', index=True)
    neto_account_balance = fields.Char(string='Neto Account Balance', readonly=True)
    neto_available_credit = fields.Char(string='Neto Available Credit', readonly=True)
    neto_on_credit_hold = fields.Boolean(string='On Credit Hold (Neto)', default=False)
    neto_classification = fields.Char(string='Neto Classification')
    neto_last_sync = fields.Datetime(string='Neto Last Sync', readonly=True)
