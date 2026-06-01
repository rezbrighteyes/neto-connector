# -*- coding: utf-8 -*-
from odoo import _, fields, models
from odoo.exceptions import UserError


class NetoProductSyncWizard(models.TransientModel):
    _name = 'neto.product.sync.wizard'
    _description = 'Run Neto Product Sync'

    store_id = fields.Many2one(
        'neto.store',
        string='Store',
        domain=[('active', '=', True)],
        help='Leave blank to sync products for all active Neto stores.',
    )
    import_active_products = fields.Boolean(string='Import Active Products', default=True)
    import_inactive_products = fields.Boolean(string='Import Inactive Products', default=False)
    update_stock_quantity = fields.Boolean(string='Update Stock Quantity', default=False)

    def action_run_product_sync(self):
        self.ensure_one()
        if not self.import_active_products and not self.import_inactive_products:
            raise UserError(_('Select active products, inactive products, or both.'))

        store_ids = [self.store_id.id] if self.store_id else None
        self.env['neto.connector'].sudo().run_product_sync(
            store_ids=store_ids,
            include_active=self.import_active_products,
            include_inactive=self.import_inactive_products,
            sync_stock=self.update_stock_quantity,
        )

        domain = []
        if self.store_id:
            domain = [('store_id', '=', self.store_id.id)]
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'neto.product.sync.log',
            'view_mode': 'list,pivot,form',
            'domain': domain,
            'target': 'current',
            'name': _('Product Sync Log'),
        }
