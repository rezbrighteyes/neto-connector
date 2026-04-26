# -*- coding: utf-8 -*-
from odoo import models, fields


class ResPartner(models.Model):
    _inherit = 'res.partner'

    neto_username = fields.Char(string='Neto Username', index=True)
