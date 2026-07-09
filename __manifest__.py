# -*- coding: utf-8 -*-
{
    'name': 'Neto Order Sync for Odoo',
    'version': '19.0.1.5.25',
    'author': 'Reza Shiraz',
    'license': 'OPL-1',
    'category': 'Sales/Sales',
    'summary': (
        'Imports wholesale B2B orders from Neto into Odoo as confirmed sale orders, '
        'with smart filtering, a sync-log dashboard, and single-order manual sync.'
    ),
    'description': """
Neto Order Sync for Odoo
========================
This module polls the Neto REST API on a configurable schedule and creates
sale.order records in Odoo for wholesale B2B orders only. Internal $0
transfers and BrightEyes replenishment orders are silently skipped.
Partners are matched by Neto Username or auto-created. A sync-log dashboard
gives full visibility into every sync run. Admins can manually sync any
single order by Neto Order ID via Neto Sync > Sync Single Order.
    """,
    'depends': ['sale', 'mail', 'account', 'stock'],
    'data': [
        'security/ir.model.access.csv',
        'data/cron.xml',
        'views/neto_store_views.xml',
        'views/neto_payment_views.xml',
        'views/neto_invoice_terms_map_views.xml',
        'views/neto_history_import_job_views.xml',
        'views/neto_price_map_views.xml',
        'views/neto_product_link_views.xml',
        'views/neto_product_sync_wizard_views.xml',
        'views/neto_product_sync_log_views.xml',
        'views/product_product_views.xml',
        'views/product_template_views.xml',
        'views/neto_sync_log_views.xml',
        'views/neto_shipping_audit_wizard_views.xml',
        'views/neto_sync_wizard_views.xml',
        'reports/neto_history_tax_invoice_report.xml',
        'views/res_partner_views.xml',
        'views/sale_order_views.xml',
        'views/res_config_settings_views.xml',
        'views/neto_rma_log_views.xml',
        'views/account_move_views.xml',
        'views/menus.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
    'post_init_hook': 'post_init_hook',
}
