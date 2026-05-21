# -*- coding: utf-8 -*-
import logging

import requests

from odoo import fields, models
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)

_GST_DIVISOR = 1.1
_PRODUCT_PAGE_SIZE = 100
_PRODUCT_WRITE_BATCH_SIZE = 50
_CATCHALL_PARENT_NAME = 'Neto'
_CATCHALL_CHILD_NAME = 'Uncategorized'
_GLE_REVIEW_TAG = 'pricing-needs-review'
_NETO_VARIANT_ATTRIBUTE = 'Neto Variant SKU'
_EDBERT_COMPANY_ID = 1
_PRODUCT_OUTPUT_SELECTOR = [
    'ID',
    'InventoryID',
    'SKU',
    'ParentSKU',
    'Name',
    'UPC',
    'UPC1',
    'RRP',
    'DefaultPrice',
    'CostPrice',
    'DefaultPurchasePrice',
    'TaxInclusive',
    'IsActive',
    'IsVariant',
    'Categories',
    'ReferenceNumber',
    'PriceGroups',
]


def _strip_leading_zeroes(value):
    value = (value or '').strip()
    if not value:
        return ''
    return value.lstrip('0') or '0'


def _to_float(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _is_tax_inclusive(item):
    return str(item.get('TaxInclusive') or '').strip().lower() == 'true'


def _to_ex_gst(amount, item):
    amount = round(_to_float(amount), 4)
    if amount <= 0:
        return 0.0
    if _is_tax_inclusive(item):
        return round(amount / _GST_DIVISOR, 4)
    return amount


def _get_sku_variants(value):
    sku = (value or '').strip()
    if not sku:
        return []
    variants = [sku]
    if sku.lower().startswith('pack_'):
        stripped = sku[5:]
        if stripped:
            variants.append(stripped)
    return variants


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    neto_parent_sku = fields.Char(string='Neto Parent SKU', copy=False, index=True)


class ProductProduct(models.Model):
    _inherit = 'product.product'

    neto_product_id = fields.Char(string='Neto Product ID', copy=False, index=True)
    neto_store_id = fields.Many2one(
        'neto.store',
        string='Neto Store',
        copy=False,
        index=True,
        ondelete='set null',
    )
    neto_parent_sku = fields.Char(string='Neto Parent SKU', copy=False, index=True)
    neto_last_product_sync = fields.Datetime(string='Neto Product Sync', copy=False, readonly=True)
    neto_product_sync_state = fields.Selection(
        [
            ('created', 'Created'),
            ('updated', 'Updated'),
            ('skipped', 'Skipped'),
            ('conflict', 'Conflict'),
            ('unmatched', 'Unmatched'),
            ('error', 'Error'),
        ],
        string='Neto Product Sync State',
        copy=False,
        readonly=True,
    )
    neto_product_sync_note = fields.Char(string='Neto Product Sync Note', copy=False, readonly=True)


class NetoProductSyncLog(models.Model):
    _name = 'neto.product.sync.log'
    _description = 'Neto Product Sync Log'
    _order = 'sync_date desc, id desc'

    store_id = fields.Many2one('neto.store', string='Store', ondelete='set null', index=True)
    neto_sku = fields.Char(string='Neto SKU', index=True)
    neto_product_id = fields.Char(string='Neto Product ID', index=True)
    product_id = fields.Many2one('product.product', string='Odoo Product', ondelete='set null', index=True)
    action = fields.Selection(
        [
            ('created', 'Created'),
            ('updated', 'Updated'),
            ('skipped', 'Skipped'),
            ('conflict', 'Conflict'),
            ('unmatched', 'Unmatched'),
            ('error', 'Error'),
        ],
        string='Action',
        required=True,
        index=True,
    )
    reason = fields.Text(string='Reason')
    sync_date = fields.Datetime(string='Timestamp', default=fields.Datetime.now, readonly=True)


class NetoConnector(models.AbstractModel):
    _inherit = 'neto.connector'

    def _fetch_products_page(self, store, page=1, limit=_PRODUCT_PAGE_SIZE):
        url = f"{store.store_url.rstrip('/')}/do/WS/NetoAPI"
        headers = {
            'Content-Type': 'application/json',
            'NETOAPI_ACTION': 'GetItem',
            'NETOAPI_KEY': store.api_key,
            'Accept': 'application/json',
        }
        payload = {
            'Filter': {
                'IsActive': 'True',
                'Page': page,
                'Limit': limit,
                'OutputSelector': _PRODUCT_OUTPUT_SELECTOR,
            }
        }
        response = requests.post(url, json=payload, headers=headers, timeout=90)
        response.raise_for_status()
        body = response.json()
        items = body.get('Item', [])
        if isinstance(items, dict):
            items = [items]
        return items or []

    def _fetch_all_active_products(self, store):
        items = []
        page = 1
        while True:
            chunk = self._fetch_products_page(store, page=page, limit=_PRODUCT_PAGE_SIZE)
            if not chunk:
                break
            items.extend(chunk)
            if len(chunk) < _PRODUCT_PAGE_SIZE:
                break
            page += 1
        return [item for item in items if str(item.get('IsActive') or '').strip().lower() == 'true']

    def _get_neto_categories(self, item):
        raw = item.get('Categories') or []
        if not raw or raw == ['']:
            return []
        names = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            category_value = entry.get('Category') or {}
            if isinstance(category_value, dict):
                category_value = [category_value]
            for category in category_value:
                if not isinstance(category, dict):
                    continue
                name = (category.get('CategoryName') or '').strip()
                if name and name not in names:
                    names.append(name)
        return names

    def _get_brighteyes_price(self, item):
        groups = item.get('PriceGroups') or []
        if not groups:
            return None
        first = groups[0] if isinstance(groups, list) else groups
        price_groups = first.get('PriceGroup', {}) if isinstance(first, dict) else {}
        if isinstance(price_groups, dict):
            price_groups = [price_groups]
        for group in price_groups:
            if group.get('Group') == 'BrightEyes Stores':
                try:
                    return float(group.get('Price') or 0)
                except (TypeError, ValueError):
                    return None
        return None

    def _get_price_group_price(self, item, group_names):
        groups = item.get('PriceGroups') or []
        if not groups:
            return None
        if isinstance(group_names, str):
            group_names = [group_names]
        first = groups[0] if isinstance(groups, list) else groups
        price_groups = first.get('PriceGroup', {}) if isinstance(first, dict) else {}
        if isinstance(price_groups, dict):
            price_groups = [price_groups]
        target_names = {name.strip().lower() for name in group_names if name}
        for group in price_groups:
            if (group.get('Group') or '').strip().lower() in target_names:
                try:
                    return float(group.get('Price') or 0)
                except (TypeError, ValueError):
                    return None
        return None

    def _is_liaise_store(self, store):
        values = [
            (store.name or '').strip().lower(),
            (store.company_id.name or '').strip().lower(),
        ]
        return any('liaise' in value for value in values)

    def _is_global_store(self, store):
        values = [
            (store.name or '').strip().lower(),
            (store.company_id.name or '').strip().lower(),
        ]
        return any('global' in value for value in values)

    def _get_or_create_catchall_category(self, store):
        if store.neto_default_categ_id:
            return store.neto_default_categ_id
        Category = self.env['product.category'].sudo()
        parent = Category.search([('name', '=', _CATCHALL_PARENT_NAME)], limit=1)
        if not parent:
            parent = Category.create({'name': _CATCHALL_PARENT_NAME})
        category = Category.search(
            [('name', '=', _CATCHALL_CHILD_NAME), ('parent_id', '=', parent.id)],
            limit=1,
        )
        if not category:
            category = Category.create({'name': _CATCHALL_CHILD_NAME, 'parent_id': parent.id})
        return category

    def _get_or_create_category(self, store, item):
        categories = self._get_neto_categories(item)
        if not categories:
            return self._get_or_create_catchall_category(store)
        category_name = categories[-1]
        Category = self.env['product.category'].sudo()
        category = Category.search([('name', '=', category_name)], limit=1)
        if category:
            return category
        return Category.create({'name': category_name})

    def _get_pricing_review_tag(self):
        Tag = self.env['product.tag'].sudo()
        tag = Tag.search([('name', '=', _GLE_REVIEW_TAG)], limit=1)
        if not tag:
            tag = Tag.create({'name': _GLE_REVIEW_TAG})
        return tag

    def _ensure_company_on_template(self, template, company):
        commands = []
        for company_id in (_EDBERT_COMPANY_ID, company.id):
            if company_id and company_id not in template.company_ids.ids:
                commands.append((4, company_id))
        if commands:
            template.sudo().write({'company_ids': commands})

    def _set_pricing_review_tag(self, template):
        if 'product_tag_ids' not in template._fields:
            return
        tag = self._get_pricing_review_tag()
        if tag.id not in template.product_tag_ids.ids:
            template.sudo().write({'product_tag_ids': [(4, tag.id)]})

    def _get_price_values(self, store, item):
        rrp_ex = _to_ex_gst(item.get('RRP'), item)
        default_price_ex = _to_ex_gst(item.get('DefaultPrice'), item)
        cost_price = round(_to_float(item.get('CostPrice')), 4)
        default_purchase_price = round(_to_float(item.get('DefaultPurchasePrice')), 4)
        resolved_cost_price = cost_price if cost_price > 0 else default_purchase_price
        if self._is_liaise_store(store):
            catalogue_price = self._get_price_group_price(item, ['Catalogue'])
            catalogue_ex = _to_ex_gst(catalogue_price, item) if catalogue_price else 0.0
            list_price = catalogue_ex or default_price_ex
        else:
            brighteyes_raw = self._get_brighteyes_price(item)
            brighteyes_ex = _to_ex_gst(brighteyes_raw, item) if brighteyes_raw else 0.0
            if brighteyes_ex > 0 and brighteyes_ex > resolved_cost_price:
                list_price = brighteyes_ex
            else:
                list_price = round(rrp_ex / 2.0, 4)
        return {
            'sale_price': list_price,
            'rrp_ex': rrp_ex,
            'cost_price': resolved_cost_price,
        }

    def _select_active_unique_match(self, products):
        if not products:
            return False, False
        active = products.filtered(lambda product: product.active)
        if len(active) == 1:
            return active, False
        if len(products) == 1:
            return products, False
        return False, True

    def _get_barcode_candidates(self, item):
        values = []
        for key in ('UPC', 'UPC1'):
            raw = (item.get(key) or '').strip()
            if raw:
                values.append(raw)
                normalized = _strip_leading_zeroes(raw)
                if normalized and normalized not in values:
                    values.append(normalized)
        return values

    def _select_barcode_match(self, products):
        if not products:
            return False, False
        active = products.filtered(lambda product: product.active)
        if len(active) == 1:
            return active, False
        if len(products) == 1:
            return products, False
        return False, True

    def _match_existing_product(self, store, item):
        Product = self.env['product.product'].sudo()
        neto_product_id = (item.get('ID') or item.get('InventoryID') or '').strip()
        sku = (item.get('SKU') or '').strip()
        sku_variants = _get_sku_variants(sku)
        if neto_product_id:
            product = Product.search([
                ('neto_store_id', '=', store.id),
                ('neto_product_id', '=', neto_product_id),
            ], limit=1)
            if product:
                return product, False
        if 'x_studio_neto_reference' in Product._fields:
            for sku_variant in sku_variants:
                products = Product.search([('x_studio_neto_reference', '=', sku_variant)])
                matched, conflict = self._select_active_unique_match(products)
                if matched or conflict:
                    return matched, conflict
        for sku_variant in sku_variants:
            products = Product.search([('default_code', '=', sku_variant)])
            matched, conflict = self._select_active_unique_match(products)
            if matched or conflict:
                return matched, conflict
        barcode_candidates = self._get_barcode_candidates(item)
        if barcode_candidates:
            products = Product.search([('barcode', 'in', barcode_candidates)])
            matched, conflict = self._select_barcode_match(products)
            if matched or conflict:
                return matched, conflict
        return False, False

    def _sync_pricelist_price(self, store, product, sale_price):
        pricelist = store.pricelist_id
        if not pricelist:
            return
        PricelistItem = self.env['product.pricelist.item'].sudo()
        item = PricelistItem.search([
            ('pricelist_id', '=', pricelist.id),
            ('applied_on', '=', '0_product_variant'),
            ('product_id', '=', product.id),
        ], limit=1)
        values = {
            'pricelist_id': pricelist.id,
            'applied_on': '0_product_variant',
            'product_id': product.id,
            'compute_price': 'fixed',
            'fixed_price': sale_price,
        }
        if item:
            item.write(values)
        else:
            PricelistItem.create(values)

    def _get_variant_attribute(self):
        Attribute = self.env['product.attribute'].sudo()
        attribute = Attribute.search([('name', '=', _NETO_VARIANT_ATTRIBUTE)], limit=1)
        if not attribute:
            attribute = Attribute.create({
                'name': _NETO_VARIANT_ATTRIBUTE,
                'create_variant': 'always',
            })
        return attribute

    def _get_or_create_parent_template(self, store, item, category):
        ProductTemplate = self.env['product.template'].sudo()
        parent_sku = (item.get('ParentSKU') or '').strip()
        template = ProductTemplate.search([
            ('neto_parent_sku', '=', parent_sku),
            ('company_ids', 'in', [store.company_id.id]),
        ], limit=1)
        if template:
            return template
        template = ProductTemplate.create({
            'name': (item.get('Name') or parent_sku or 'Neto Product').strip(),
            'categ_id': category.id,
            'company_ids': [(4, _EDBERT_COMPANY_ID), (4, store.company_id.id)],
            'neto_parent_sku': parent_sku,
            'sale_ok': True,
            'purchase_ok': True,
            'active': True,
        })
        return template

    def _get_or_create_variant_product(self, store, template, item, category):
        attribute = self._get_variant_attribute()
        Value = self.env['product.attribute.value'].sudo()
        sku = (item.get('SKU') or '').strip()
        value = Value.search([
            ('attribute_id', '=', attribute.id),
            ('name', '=', sku),
        ], limit=1)
        if not value:
            value = Value.create({
                'attribute_id': attribute.id,
                'name': sku,
            })
        line = template.attribute_line_ids.filtered(lambda record: record.attribute_id == attribute)
        if line:
            if value.id not in line.value_ids.ids:
                line.write({'value_ids': [(4, value.id)]})
        else:
            template.write({
                'attribute_line_ids': [(0, 0, {
                    'attribute_id': attribute.id,
                    'value_ids': [(6, 0, [value.id])],
                })]
            })
        product = self.env['product.product'].sudo().search([
            ('product_tmpl_id', '=', template.id),
            ('product_template_attribute_value_ids.product_attribute_value_id', '=', value.id),
        ], limit=1)
        if not product:
            # As a last resort, reuse the template's generated variant.
            product = template.product_variant_ids[:1]
        if category and template.categ_id != category:
            template.write({'categ_id': category.id})
        self._ensure_company_on_template(template, store.company_id)
        return product

    def _get_or_create_product(self, store, item, category):
        is_variant = str(item.get('IsVariant') or '').strip().lower() == 'true'
        parent_sku = (item.get('ParentSKU') or '').strip()
        if is_variant and parent_sku and parent_sku != '0':
            template = self._get_or_create_parent_template(store, item, category)
            return self._get_or_create_variant_product(store, template, item, category), template
        ProductTemplate = self.env['product.template'].sudo()
        template = ProductTemplate.create({
            'name': (item.get('Name') or item.get('SKU') or 'Neto Product').strip(),
            'categ_id': category.id,
            'company_ids': [(4, _EDBERT_COMPANY_ID), (4, store.company_id.id)],
            'sale_ok': True,
            'purchase_ok': True,
            'active': True,
        })
        return template.product_variant_ids[:1], template

    def _prepare_product_write_values(self, store, item, price_values, action, reason=False, product=False):
        sku = (item.get('SKU') or '').strip()
        barcode = next((value for value in self._get_barcode_candidates(item) if value), False)
        neto_product_id = (item.get('ID') or item.get('InventoryID') or '').strip()
        parent_sku = (item.get('ParentSKU') or '').strip()
        values = {
            'barcode': barcode or False,
            'recommended_retail_price': price_values['rrp_ex'],
            'standard_price': price_values['cost_price'],
            'neto_product_id': neto_product_id or False,
            'neto_store_id': store.id,
            'neto_parent_sku': parent_sku or False,
            'neto_last_product_sync': fields.Datetime.now(),
            'neto_product_sync_state': action,
            'neto_product_sync_note': reason or False,
        }
        # Preserve an existing Odoo internal reference on matched products.
        if not product or not product.default_code:
            values['default_code'] = sku or False
        if 'x_studio_neto_reference' in self.env['product.product']._fields:
            values['x_studio_neto_reference'] = sku or False
        return values

    def _is_barcode_conflict(self, exc):
        return isinstance(exc, ValidationError) and 'Barcode(s) already assigned' in str(exc)

    def _write_product_record(self, product, template, store, item, category, action, reason=False):
        price_values = self._get_price_values(store, item)
        is_variant = str(item.get('IsVariant') or '').strip().lower() == 'true'
        if price_values['cost_price'] > 0:
            template.sudo().with_company(store.company_id).write({
                'standard_price': price_values['cost_price'],
            })
        template_values = {
            'categ_id': category.id,
            'active': True,
        }
        if not store.pricelist_id:
            template_values['list_price'] = price_values['sale_price']
        if not is_variant:
            template_values['name'] = (item.get('Name') or item.get('SKU') or template.name).strip()
        template.sudo().write(template_values)
        self._ensure_company_on_template(template, store.company_id)
        product_values = self._prepare_product_write_values(
            store, item, price_values, action, reason=reason, product=product,
        )
        try:
            product.sudo().write(product_values)
        except ValidationError as exc:
            if not self._is_barcode_conflict(exc):
                raise
            barcode_value = product_values.pop('barcode', False)
            retry_reason = f'Barcode conflict on {barcode_value}; kept existing Odoo barcode'
            product_values['neto_product_sync_note'] = retry_reason
            product.sudo().write(product_values)
            self._log_product_sync(store, item, 'skipped', product=product, reason=retry_reason)
        self._sync_pricelist_price(store, product, price_values['sale_price'])
        if self._is_global_store(store):
            self._set_pricing_review_tag(template)

    def _log_product_sync(self, store, item, action, product=False, reason=False):
        values = {
            'store_id': store.id if store else False,
            'neto_sku': (item.get('SKU') or '').strip() if item else False,
            'neto_product_id': (item.get('ID') or item.get('InventoryID') or '').strip() if item else False,
            'product_id': product.id if product else False,
            'action': action,
            'reason': reason or False,
        }
        self.env['neto.product.sync.log'].sudo().create(values)

    def _process_product_item(self, store, item, matched_ids):
        product, conflict = self._match_existing_product(store, item)
        if conflict:
            self._log_product_sync(store, item, 'conflict', reason='Ambiguous existing Odoo match')
            return False
        category = self._get_or_create_category(store, item)
        if product:
            template = product.product_tmpl_id
            self._write_product_record(product, template, store, item, category, 'updated')
            matched_ids.add(product.id)
            self._log_product_sync(store, item, 'updated', product=product)
            return product
        product, template = self._get_or_create_product(store, item, category)
        if not product:
            self._log_product_sync(store, item, 'error', reason='Unable to create Odoo product')
            return False
        self._write_product_record(product, template, store, item, category, 'created')
        matched_ids.add(product.id)
        self._log_product_sync(store, item, 'created', product=product)
        return product

    def _split_products(self, items):
        standalones = []
        variants = []
        for item in items:
            is_variant = str(item.get('IsVariant') or '').strip().lower() == 'true'
            parent_sku = (item.get('ParentSKU') or '').strip()
            if is_variant and parent_sku and parent_sku != '0':
                variants.append(item)
            else:
                standalones.append(item)
        return standalones, variants

    def _collect_active_signatures(self, items):
        signatures = {
            'neto_ids': set(),
            'skus': set(),
            'barcodes': set(),
        }
        for item in items:
            neto_id = (item.get('ID') or item.get('InventoryID') or '').strip()
            sku = (item.get('SKU') or '').strip()
            if neto_id:
                signatures['neto_ids'].add(neto_id)
            if sku:
                signatures['skus'].add(sku)
            for barcode in self._get_barcode_candidates(item):
                if barcode:
                    signatures['barcodes'].add(barcode)
                    normalized = _strip_leading_zeroes(barcode)
                    if normalized:
                        signatures['barcodes'].add(normalized)
        return signatures

    def _log_unmatched_products(self, stores, matched_ids_by_store, signatures_by_store):
        Product = self.env['product.product'].sudo()
        store_company_ids = stores.mapped('company_id').ids
        if not store_company_ids:
            return
        products = Product.search([
            ('active', '=', True),
            ('product_tmpl_id.company_ids', 'in', store_company_ids),
        ])
        for product in products:
            if product.id in matched_ids_by_store:
                continue
            related_store = product.neto_store_id
            if not related_store:
                related_store = stores.filtered(
                    lambda store: store.company_id.id in product.product_tmpl_id.company_ids.ids
                )[:1]
            if not related_store:
                continue
            signatures = signatures_by_store.get(related_store.id, {})
            barcode_values = {
                value for value in [
                    (product.barcode or '').strip(),
                    _strip_leading_zeroes(product.barcode or ''),
                ] if value
            }
            if product.neto_product_id and product.neto_product_id in signatures.get('neto_ids', set()):
                continue
            if product.default_code and product.default_code in signatures.get('skus', set()):
                continue
            if 'x_studio_neto_reference' in product._fields:
                ref_value = (product.x_studio_neto_reference or '').strip()
                if ref_value and ref_value in signatures.get('skus', set()):
                    continue
            if barcode_values & signatures.get('barcodes', set()):
                continue
            reason = 'Active Odoo product not matched to any active Neto item for this store'
            self._log_product_sync(related_store, {
                'SKU': product.default_code or '',
                'ID': product.neto_product_id or '',
            }, 'unmatched', product=product, reason=reason)
            product.sudo().write({
                'neto_last_product_sync': fields.Datetime.now(),
                'neto_product_sync_state': 'unmatched',
                'neto_product_sync_note': reason,
            })

    def _sync_product_store(self, store):
        if not store.api_key or not store.store_url:
            _logger.warning(
                'Neto product sync: store "%s" missing api_key or store_url — skipping.',
                store.name,
            )
            return set(), {}
        _logger.info('Neto product sync [%s]: fetching active products', store.name)
        items = self._fetch_all_active_products(store)
        standalones, variants = self._split_products(items)
        matched_ids = set()
        write_counter = 0
        for item in standalones + variants:
            try:
                product = self._process_product_item(store, item, matched_ids)
                if product:
                    write_counter += 1
                    if write_counter % _PRODUCT_WRITE_BATCH_SIZE == 0:
                        self.env.cr.commit()
            except Exception as exc:
                _logger.exception(
                    'Neto product sync [%s]: failed on SKU %s',
                    store.name, item.get('SKU'),
                )
                self._log_product_sync(store, item, 'error', reason=str(exc))
                self.env.cr.commit()
        store.sudo().write({'last_product_sync_date': fields.Datetime.now()})
        _logger.info(
            'Neto product sync [%s]: completed — %d active item(s), %d matched/created.',
            store.name, len(items), len(matched_ids),
        )
        return matched_ids, self._collect_active_signatures(items)

    def run_product_sync(self, store_ids=None):
        domain = [('active', '=', True)]
        if store_ids:
            domain.append(('id', 'in', store_ids))
        stores = self.env['neto.store'].sudo().search(domain)
        if not stores:
            _logger.warning('Neto product sync: no active stores configured — aborting.')
            return
        all_matched_ids = set()
        signatures_by_store = {}
        for store in stores:
            try:
                matched_ids, signatures = self._sync_product_store(store)
                all_matched_ids.update(matched_ids)
                signatures_by_store[store.id] = signatures
            except Exception as exc:
                _logger.exception(
                    'Neto product sync: store "%s" failed — %s',
                    store.name, exc,
                )
        self._log_unmatched_products(stores, all_matched_ids, signatures_by_store)
