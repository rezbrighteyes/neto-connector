# -*- coding: utf-8 -*-
from odoo import api, SUPERUSER_ID


def post_init_hook(*args):
    if len(args) == 1:
        env = args[0]
    else:
        cr, registry = args
        env = api.Environment(cr, SUPERUSER_ID, {})
    env['neto.connector'].sudo().backfill_legacy_product_links()
