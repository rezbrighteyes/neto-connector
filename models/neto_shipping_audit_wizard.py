# -*- coding: utf-8 -*-
import base64
import csv
import io
import logging

import requests

from odoo import _, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

_GETITEM_PAGE_SIZE = 500
_GETITEM_OUTPUT = [
    'SKU',
    'Name',
    'Brand',
    'Model',
    'UPC',
    'UPC1',
    'IsActive',
    'ShippingWeight',
    'ShippingLength',
    'ShippingWidth',
    'ShippingHeight',
    'ItemLength',
    'ItemWidth',
    'ItemHeight',
]


class NetoShippingAuditWizard(models.TransientModel):
    _name = 'neto.shipping.audit.wizard'
    _description = 'Audit Neto Product Shipping Data'

    store_ids = fields.Many2many(
        'neto.store',
        string='Stores',
        domain=[('active', '=', True)],
        default=lambda self: self.env['neto.store'].search([('active', '=', True)]),
        help='Neto stores to audit. Use both GLE and LIA when comparing the full catalogue.',
    )
    sku_input = fields.Text(
        string='Specific SKUs',
        help='Optional. Enter one SKU per line or comma separated. Leave empty to audit all items returned by Neto.',
    )
    limit = fields.Integer(
        string='Limit',
        default=0,
        help='Optional safety limit across each store. Leave 0 to fetch all matching Neto items.',
    )
    include_inactive = fields.Boolean(
        string='Include Inactive Neto Items',
        default=False,
    )
    csv_file = fields.Binary(string='Audit CSV', readonly=True)
    csv_filename = fields.Char(string='Filename', readonly=True)
    result_message = fields.Text(string='Result', readonly=True)

    def _float_or_zero(self, value):
        if value in (None, False, ''):
            return 0.0
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def _normalise_sku_list(self):
        raw = self.sku_input or ''
        values = []
        for chunk in raw.replace(',', '\n').splitlines():
            sku = chunk.strip()
            if sku:
                values.append(sku)
        return values

    def _extract_items(self, body):
        if 'GetItemResponse' in body:
            items = body['GetItemResponse'].get('Item', [])
        else:
            items = body.get('Item', [])
        if isinstance(items, dict):
            items = [items]
        return items or []

    def _fetch_neto_items(self, store, skus):
        url = f"{store.store_url.rstrip('/')}/do/WS/NetoAPI"
        headers = {
            'Content-Type': 'application/json',
            'NETOAPI_ACTION': 'GetItem',
            'NETOAPI_KEY': store.api_key,
            'Accept': 'application/json',
        }

        if skus:
            payload = {
                'Filter': {
                    'SKU': skus,
                    'OutputSelector': _GETITEM_OUTPUT,
                }
            }
            return self._post_getitem(store, url, headers, payload)

        all_items = []
        page = 1
        while True:
            payload = {
                'Filter': {
                    'Page': page,
                    'Limit': _GETITEM_PAGE_SIZE,
                    'OutputSelector': _GETITEM_OUTPUT,
                }
            }
            items = self._post_getitem(store, url, headers, payload)
            all_items.extend(items)
            if self.limit and len(all_items) >= self.limit:
                return all_items[:self.limit]
            if len(items) < _GETITEM_PAGE_SIZE:
                return all_items
            page += 1

    def _post_getitem(self, store, url, headers, payload):
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=90)
            response.raise_for_status()
            body = response.json()
        except Exception as exc:
            raise UserError(_('Neto GetItem failed for store "%s": %s') % (store.name, exc))
        items = self._extract_items(body)
        _logger.info('Neto shipping audit [%s]: fetched %d item(s)', store.name, len(items))
        return items

    def _find_odoo_product(self, item):
        Product = self.env['product.product'].sudo()
        sku = (item.get('SKU') or '').strip()
        product = Product.browse()
        match_type = ''
        if sku:
            product = Product.search([('default_code', '=', sku)], limit=2)
            match_type = 'SKU' if len(product) == 1 else ('duplicate_sku' if product else '')
        if not product or len(product) != 1:
            barcode = (item.get('UPC') or item.get('UPC1') or '').strip()
            if barcode:
                barcode_match = Product.search([('barcode', '=', barcode)], limit=2)
                if len(barcode_match) == 1:
                    product = barcode_match
                    match_type = 'barcode'
                elif len(barcode_match) > 1:
                    product = barcode_match
                    match_type = 'duplicate_barcode'
        return product, match_type

    def _row_for_item(self, store, item):
        product, match_type = self._find_odoo_product(item)
        single_product = product if len(product) == 1 else self.env['product.product']
        package = single_product.packaging_id if single_product and 'packaging_id' in single_product._fields else False

        neto_weight = self._float_or_zero(item.get('ShippingWeight'))
        neto_length = self._float_or_zero(item.get('ShippingLength') or item.get('ItemLength'))
        neto_width = self._float_or_zero(item.get('ShippingWidth') or item.get('ItemWidth'))
        neto_height = self._float_or_zero(item.get('ShippingHeight') or item.get('ItemHeight'))
        odoo_weight = single_product.weight if single_product else 0.0

        notes = []
        if match_type.startswith('duplicate'):
            notes.append(match_type)
        elif not single_product:
            notes.append('no_odoo_product_match')
        if not neto_weight:
            notes.append('missing_neto_weight')
        if single_product and not odoo_weight:
            notes.append('missing_odoo_weight')
        if neto_weight and odoo_weight and abs(neto_weight - odoo_weight) > 0.001:
            notes.append('weight_differs')
        if not (neto_length and neto_width and neto_height):
            notes.append('missing_neto_dimensions')
        if package and (package.length or package.width or package.height):
            notes.append('odoo_default_package_exists')

        return {
            'store': store.name,
            'company': store.company_id.display_name,
            'neto_sku': (item.get('SKU') or '').strip(),
            'neto_name': (item.get('Name') or '').strip(),
            'neto_brand': (item.get('Brand') or '').strip(),
            'neto_model': (item.get('Model') or '').strip(),
            'neto_barcode': (item.get('UPC') or item.get('UPC1') or '').strip(),
            'neto_active': item.get('IsActive'),
            'match_type': match_type,
            'odoo_product_id': single_product.id if single_product else '',
            'odoo_product': single_product.display_name if single_product else '',
            'odoo_sku': single_product.default_code if single_product else '',
            'odoo_barcode': single_product.barcode if single_product else '',
            'odoo_weight_kg': odoo_weight if single_product else '',
            'neto_shipping_weight_kg': neto_weight,
            'proposed_weight_kg': neto_weight or '',
            'weight_delta_kg': round(neto_weight - odoo_weight, 4) if single_product and neto_weight else '',
            'neto_length_cm': neto_length,
            'neto_width_cm': neto_width,
            'neto_height_cm': neto_height,
            'odoo_default_package': package.display_name if package else '',
            'odoo_package_length': package.length if package else '',
            'odoo_package_width': package.width if package else '',
            'odoo_package_height': package.height if package else '',
            'notes': ';'.join(notes),
        }

    def action_generate_audit(self):
        self.ensure_one()
        stores = self.store_ids.filtered('active')
        if not stores:
            raise UserError(_('Please select at least one active Neto store.'))

        skus = self._normalise_sku_list()
        rows = []
        for store in stores:
            items = self._fetch_neto_items(store, skus)
            for item in items:
                if not self.include_inactive:
                    active_raw = str(item.get('IsActive') or '').strip().lower()
                    if active_raw in ('false', '0', 'no'):
                        continue
                rows.append(self._row_for_item(store, item))

        if not rows:
            raise UserError(_('No Neto items were returned for the selected filters.'))

        fieldnames = [
            'store', 'company', 'neto_sku', 'neto_name', 'neto_brand', 'neto_model',
            'neto_barcode', 'neto_active', 'match_type', 'odoo_product_id',
            'odoo_product', 'odoo_sku', 'odoo_barcode', 'odoo_weight_kg',
            'neto_shipping_weight_kg', 'proposed_weight_kg', 'weight_delta_kg',
            'neto_length_cm', 'neto_width_cm', 'neto_height_cm',
            'odoo_default_package', 'odoo_package_length', 'odoo_package_width',
            'odoo_package_height', 'notes',
        ]
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

        filename = 'neto_shipping_audit.csv'
        self.write({
            'csv_file': base64.b64encode(buffer.getvalue().encode('utf-8')),
            'csv_filename': filename,
            'result_message': _('Generated %s row(s). Review the CSV before importing anything.') % len(rows),
        })

        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }
