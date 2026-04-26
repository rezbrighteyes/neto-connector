# -*- coding: utf-8 -*-
from odoo import models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    def action_open_neto_stores(self):
        return self.env['ir.actions.act_window']._for_xml_id(
            'Reza_neto_connector.neto_store_action'
        )
