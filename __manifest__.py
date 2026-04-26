# -*- coding: utf-8 -*-
{
    'name': 'Neto Order Sync for Odoo',
    'version': '19.0.1.0.0',
    'author': 'Reza Shiraz',
    'license': 'OPL-1',
    'category': 'Sales/Sales',
    'summary': (
        'Imports wholesale B2B orders from Neto into Odoo as confirmed sale orders, '
        'with smart filtering and a sync-log dashboard.'
    ),
    'description': """
Neto Order Sync for Odoo
========================
This module polls the Neto REST API on a configurable schedule and creates
sale.order records in Odoo for wholesale B2B orders only. Internal $0
transfers and BrightEyes replenishment orders are silently skipped.
Partners are matched by Neto Username or auto-created. A sync-log dashboard
gives full visibility into every sync run.
    """,
    'depends': ['sale', 'mail'],
    'data': [
        'security/ir.model.access.csv',
        'data/cron.xml',
        'views/neto_store_views.xml',
        'views/neto_sync_log_views.xml',
        'views/res_partner_views.xml',
        'views/sale_order_views.xml',
        'views/res_config_settings_views.xml',
        'views/menus.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
