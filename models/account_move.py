# -*- coding: utf-8 -*-
from odoo import models, fields


class AccountMove(models.Model):
    _inherit = "account.move"

    neto_rma_id = fields.Char(string="Neto RMA ID", index=True, copy=False)
    neto_rma_status = fields.Char(string="Neto RMA Status", copy=False, readonly=True)
