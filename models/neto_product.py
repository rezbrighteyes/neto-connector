# -*- coding: utf-8 -*-
import json
import logging
import time
from difflib import SequenceMatcher

from psycopg2 import errors as pg_errors
import requests

from odoo import api, fields, models
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)


class NetoApiError(Exception):
    """Neto answered with HTTP 200 but Ack='Error'. Raised so a soft API failure
    can never be mistaken for an empty result set."""


_GST_DIVISOR = 1.1
_PRODUCT_PAGE_SIZE = 100
_PRODUCT_WRITE_BATCH_SIZE = 50
_SALEABLE_CATEGORY_ROOT = ('All', 'Saleable')
_CATCHALL_CATEGORY_NAME = 'Neto'
_GLE_REVIEW_TAG = 'pricing-needs-review'
_EDBERT_COMPANY_ID = 1

# Neto only returns what you ask for, and it rejects the WHOLE request with
# Ack='Error' (over HTTP 200) if any OutputSelector name is unknown. These are the
# selectors this connector has been running in production against both stores.
# Do not add to this list without confirming the name against the live API.
_PRODUCT_OUTPUT_SELECTOR_CORE = [
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
    'WarehouseQuantity',
    'AvailableSellQuantity',
    'CommittedQuantity',
    'Categories',
    'ReferenceNumber',
    'PriceGroups',
    'Model',
    'Brand',
]

# Reference-only extras, mirrored onto neto.product.link and never mapped to an
# Odoo field. UNVERIFIED against the live API. _fetch_all_products probes this
# list with a 1-item request before each run; if Neto rejects it, the run falls
# back to the core list and logs which selectors were dropped, so a wrong name
# here costs empty reference fields, never a failed or empty sync.
_PRODUCT_OUTPUT_SELECTOR_EXTRA = [
    'Subtitle',
    'PromotionPrice',
    'ShippingWeight',
    'ShippingHeight',
    'ShippingWidth',
    'ShippingLength',
    'CubicWeight',
    'PrimarySupplier',
    'SupplierItemCode',
    'Description',
    'ShortDescription',
    'ItemSpecifics',
    'SearchKeywords',
    'SEOPageTitle',
    'SEOMetaDescription',
    'DateAdded',
    'DateUpdated',
    'Approved',
    'Virtual',
]

_PRODUCT_OUTPUT_SELECTOR = _PRODUCT_OUTPUT_SELECTOR_CORE + _PRODUCT_OUTPUT_SELECTOR_EXTRA


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


def _to_bool(value):
    """Neto returns booleans as the strings 'True'/'False'. Plain bool('False')
    is True, so every one of these fields would read as set."""
    if isinstance(value, bool):
        return value
    return str(value or '').strip().lower() in ('true', '1', 'y', 'yes')


def _clean_str(value):
    if value in (None, '', [], {}):
        return False
    return str(value).strip() or False


def _dump_json(value, pretty=False):
    """Categories/PriceGroups/Specifications come back as nested dicts or lists.
    Store them as JSON rather than str(dict), which is not machine-readable."""
    if value in (None, '', [], {}):
        return False
    if isinstance(value, str):
        return value.strip() or False
    try:
        if pretty:
            return json.dumps(value, indent=2, sort_keys=True, default=str)
        return json.dumps(value, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(value)


def _to_optional_float(value):
    if value in (None, ''):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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


def _normalize_identifier(value):
    value = (value or '').strip()
    if not value:
        return ''
    return _strip_leading_zeroes(value)


def _normalize_name(value):
    return ' '.join((value or '').lower().replace('|', ' ').replace('/', ' ').split())


def _name_similarity(left, right):
    left = _normalize_name(left)
    right = _normalize_name(right)
    if not left or not right:
        return 1.0
    return SequenceMatcher(None, left, right).ratio()


def _get_neto_individual_barcode(item):
    return (item.get('UPC') or '').strip()


def _get_neto_generic_barcode(item):
    return (item.get('UPC1') or '').strip()


def _get_neto_reference_candidates(item):
    values = []
    raw = (item.get('SKU') or '').strip()
    if raw and raw != '0':
        values.append(raw)
    normalized = _strip_leading_zeroes(raw)
    if normalized and normalized != '0' and normalized not in values:
        values.append(normalized)
    return values


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    neto_parent_sku = fields.Char(string='Neto Parent SKU', copy=False, index=True)
    # The catalogue is flat -- one variant per template -- so the template's links
    # are its single variant's links. Exposed here because the template form is
    # what people actually open.
    neto_product_link_ids = fields.One2many(
        'neto.product.link',
        string='Neto Links',
        compute='_compute_neto_product_link_ids',
    )

    @api.depends('product_variant_ids.neto_product_link_ids')
    def _compute_neto_product_link_ids(self):
        for template in self:
            template.neto_product_link_ids = template.product_variant_ids.neto_product_link_ids


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
    # Order sync creates a product when an order line references a SKU that product
    # sync has not mapped yet. This used to be signalled by appending
    # '[NETO-UNSYNCED]' to the product name, which polluted every quote, invoice and
    # picking the product appeared on. The provenance now lives here instead.
    neto_auto_created_from_order = fields.Boolean(
        string='Auto-created from Neto Order',
        copy=False,
        readonly=True,
        index=True,
        help='Created by order sync because no product matched the Neto SKU. '
             'Review its price, category and tax before trusting it.',
    )
    neto_available_sell_quantity = fields.Float(
        string='Neto Available Sell Qty',
        copy=False,
        readonly=True,
    )
    neto_product_link_ids = fields.One2many(
        'neto.product.link',
        'product_id',
        string='Neto Product Links',
        readonly=True,
    )


class NetoProductLink(models.Model):
    _name = 'neto.product.link'
    _description = 'Neto Product Link'
    _order = 'store_id, neto_sku, neto_product_id, id'

    product_id = fields.Many2one(
        'product.product',
        string='Odoo Product',
        required=True,
        index=True,
        ondelete='cascade',
    )
    store_id = fields.Many2one(
        'neto.store',
        string='Store',
        required=True,
        index=True,
        ondelete='cascade',
    )
    company_id = fields.Many2one(
        'res.company',
        string='Company',
        related='store_id.company_id',
        store=True,
        index=True,
    )
    # --- identity (the reason this model exists) ---------------------------
    # Neto product IDs are per-store: Liaise and Global give different IDs to the
    # same physical item. unique(store_id, neto_product_id) below is the only
    # trustworthy Neto<->Odoo key. Never key on SKU or barcode.
    neto_product_id = fields.Char(string='Neto Product ID', index=True)
    neto_sku = fields.Char(string='Neto SKU', index=True)
    neto_barcode = fields.Char(string='Neto Barcode', index=True)
    neto_generic_barcode = fields.Char(string='Neto Generic Barcode', index=True)
    neto_parent_sku = fields.Char(string='Neto Parent SKU', index=True)
    neto_is_variant = fields.Boolean(string='Is Variant in Neto')
    neto_reference_number = fields.Char(string='Neto Reference Number')

    # --- naming / classification -------------------------------------------
    neto_name = fields.Char(string='Neto Name')
    neto_subtitle = fields.Char(string='Neto Subtitle')
    neto_model = fields.Char(string='Neto Model')
    neto_brand = fields.Char(string='Neto Brand')
    neto_categories = fields.Text(string='Neto Categories')

    # --- pricing (reference only; Odoo pricing is set elsewhere) ------------
    neto_default_price = fields.Float(string='Neto Default Price')
    neto_rrp = fields.Float(string='Neto RRP')
    neto_cost_price = fields.Float(string='Neto Cost Price')
    neto_purchase_price = fields.Float(string='Neto Purchase Price')
    neto_promotion_price = fields.Float(string='Neto Promotion Price')
    neto_tax_inclusive = fields.Boolean(string='Neto Price Tax Inclusive')
    neto_price_groups_json = fields.Text(string='Neto Price Groups JSON')

    # --- logistics ----------------------------------------------------------
    neto_shipping_weight = fields.Float(string='Neto Shipping Weight')
    neto_shipping_height = fields.Float(string='Neto Shipping Height')
    neto_shipping_width = fields.Float(string='Neto Shipping Width')
    neto_shipping_length = fields.Float(string='Neto Shipping Length')
    neto_cubic_weight = fields.Float(string='Neto Cubic Weight')

    # --- supply -------------------------------------------------------------
    neto_primary_supplier = fields.Char(string='Neto Primary Supplier')
    neto_supplier_item_code = fields.Char(string='Neto Supplier Item Code')

    # --- content ------------------------------------------------------------
    neto_description = fields.Html(string='Neto Description', sanitize=False)
    neto_short_description = fields.Html(string='Neto Short Description', sanitize=False)
    # ItemSpecifics comes back as name/value pairs, not HTML. Stored as JSON.
    neto_specifications = fields.Text(string='Neto Item Specifics (JSON)')
    neto_search_keywords = fields.Text(string='Neto Search Keywords')
    neto_seo_page_title = fields.Char(string='Neto SEO Page Title')
    neto_seo_meta_description = fields.Text(string='Neto SEO Meta Description')

    # --- audit --------------------------------------------------------------
    neto_date_added = fields.Char(string='Neto Date Added')
    neto_date_updated = fields.Char(string='Neto Date Updated')
    neto_approved = fields.Boolean(string='Approved in Neto')
    neto_virtual = fields.Boolean(string='Virtual in Neto')

    # Verbatim GetItem response for this item. The typed fields above are a
    # convenience; this is the source of truth when Neto adds a field we do not
    # model yet, or when a value needs to be argued about after the fact.
    neto_raw_json = fields.Text(string='Neto Raw JSON')

    is_active = fields.Boolean(string='Active in Neto', default=True)
    available_sell_quantity = fields.Float(string='Available Sell Qty')
    warehouse_quantity_json = fields.Text(string='Warehouse Quantity JSON')
    last_sync_date = fields.Datetime(string='Last Sync Date')
    last_sync_note = fields.Text(string='Last Sync Note')

    _neto_product_link_store_product_id_uniq = models.Constraint(
        'unique(store_id, neto_product_id)',
        'A Neto product ID can only be linked once per store.',
    )

    @api.constrains('store_id', 'neto_sku')
    def _check_unique_store_sku(self):
        for link in self:
            sku = (link.neto_sku or '').strip()
            if not link.store_id or not sku:
                continue
            duplicate = self.search([
                ('id', '!=', link.id),
                ('store_id', '=', link.store_id.id),
                ('neto_sku', '=', sku),
            ], limit=1)
            if duplicate:
                raise ValidationError(
                    'Neto SKU %s is already linked for store %s.'
                    % (sku, link.store_id.display_name)
                )

    def action_backfill_legacy_product_links(self):
        return self.env['neto.connector'].sudo().backfill_legacy_product_links()


class NetoProductSyncLog(models.Model):
    _name = 'neto.product.sync.log'
    _description = 'Neto Product Sync Log'
    _order = 'sync_date desc, id desc'

    store_id = fields.Many2one('neto.store', string='Store', ondelete='set null', index=True)
    neto_sku = fields.Char(string='Neto SKU', index=True)
    neto_product_id = fields.Char(string='Neto Product ID', index=True)
    link_id = fields.Many2one('neto.product.link', string='Product Link', ondelete='set null', index=True)
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

    def _check_neto_ack(self, body):
        """Neto signals failure with Ack='Error' inside an HTTP 200 body. Without
        this check an API error is indistinguishable from an empty catalogue --
        and an empty catalogue makes _zero_absent_store_stock zero every product
        in the store."""
        if not isinstance(body, dict):
            raise NetoApiError('Neto returned a non-object response: %r' % (body,))
        payload = body.get('GetItemResponse', body)
        ack = str(payload.get('Ack') or '').strip().lower()
        if ack == 'error':
            messages = payload.get('Messages') or body.get('Messages') or {}
            raise NetoApiError('Neto GetItem returned Ack=Error: %s' % json.dumps(messages, default=str))

    def _extract_items_from_getitem_response(self, body):
        if not isinstance(body, dict):
            return []
        if 'GetItemResponse' in body:
            items = body['GetItemResponse'].get('Item', [])
        else:
            items = body.get('Item', [])
        if isinstance(items, dict):
            items = [items]
        return items or []

    def _fetch_products_page(self, store, page=1, limit=_PRODUCT_PAGE_SIZE, active_filter='True',
                             output_selector=None):
        url = f"{store.store_url.rstrip('/')}/do/WS/NetoAPI"
        headers = {
            'Content-Type': 'application/json',
            'NETOAPI_ACTION': 'GetItem',
            'NETOAPI_KEY': store.api_key,
            'Accept': 'application/json',
        }
        payload = {
            'Filter': {
                'Page': page,
                'Limit': limit,
                'OutputSelector': output_selector or _PRODUCT_OUTPUT_SELECTOR,
            }
        }
        if active_filter in ('True', 'False'):
            payload['Filter']['IsActive'] = active_filter
        response = requests.post(url, json=payload, headers=headers, timeout=90)
        response.raise_for_status()
        body = response.json()
        self._check_neto_ack(body)
        return self._extract_items_from_getitem_response(body)

    def _fetch_all_products(self, store, include_active=True, include_inactive=False):
        items = []
        active_filters = []
        if include_active:
            active_filters.append('True')
        if include_inactive:
            active_filters.append('False')
        # Probe the extended selector list once. If Neto rejects it, fall back to
        # the core list rather than letting one unknown field name abort the sync.
        selector = _PRODUCT_OUTPUT_SELECTOR
        try:
            self._fetch_products_page(store, page=1, limit=1, active_filter='True',
                                      output_selector=selector)
        except NetoApiError as exc:
            _logger.warning(
                'Neto product sync [%s]: extended OutputSelector rejected (%s). '
                'Falling back to the core selector list; reference fields %s will be empty.',
                store.name, exc, ', '.join(_PRODUCT_OUTPUT_SELECTOR_EXTRA),
            )
            selector = _PRODUCT_OUTPUT_SELECTOR_CORE
        for active_filter in active_filters:
            page = 1
            while True:
                chunk = self._fetch_products_page(
                    store, page=page, limit=_PRODUCT_PAGE_SIZE, active_filter=active_filter,
                    output_selector=selector,
                )
                if not chunk:
                    break
                items.extend(chunk)
                if len(chunk) < _PRODUCT_PAGE_SIZE:
                    break
                page += 1
        return items

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

    def _get_inventory_candidates(self, item):
        candidates = []
        for key in ('InventoryID', 'UPC', 'UPC1', 'SKU', 'ParentSKU'):
            raw = (item.get(key) or '').strip()
            if not raw:
                continue
            if raw not in candidates:
                candidates.append(raw)
            normalized = _normalize_identifier(raw)
            if normalized and normalized not in candidates:
                candidates.append(normalized)
        return candidates

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

    def _get_or_create_category_path(self, names):
        Category = self.env['product.category'].sudo()
        parent = False
        category = False
        for name in names:
            name = (name or '').strip()
            if not name:
                continue
            domain = [('name', '=', name)]
            if parent:
                domain.append(('parent_id', '=', parent.id))
            else:
                domain.append(('parent_id', '=', False))
            category = Category.search(domain, limit=1)
            if not category:
                values = {'name': name}
                if parent:
                    values['parent_id'] = parent.id
                category = Category.create(values)
            parent = category
        return category

    def _normalize_neto_category_path(self, categories):
        cleaned = []
        for category in categories:
            name = (category or '').strip()
            if name and name not in cleaned:
                cleaned.append(name)
        while cleaned and cleaned[0].strip().lower() == 'all':
            cleaned.pop(0)
        while cleaned and cleaned[0].strip().lower() == 'saleable':
            cleaned.pop(0)
        if not cleaned:
            cleaned = [_CATCHALL_CATEGORY_NAME]
        return list(_SALEABLE_CATEGORY_ROOT) + cleaned

    def _get_or_create_catchall_category(self, store):
        if store.neto_default_categ_id:
            return store.neto_default_categ_id
        return self._get_or_create_category_path(
            list(_SALEABLE_CATEGORY_ROOT) + [_CATCHALL_CATEGORY_NAME]
        )

    def _get_or_create_category(self, store, item):
        categories = self._get_neto_categories(item)
        if not categories:
            return self._get_or_create_catchall_category(store)
        return self._get_or_create_category_path(
            self._normalize_neto_category_path(categories)
        )

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

    def _get_imported_price_map(self, store, item):
        PriceMap = self.env['neto.price.map'].sudo()
        candidates = self._get_inventory_candidates(item)
        if not candidates:
            return False
        maps = PriceMap.search([
            ('active', '=', True),
            ('inventory_id', 'in', candidates),
            '|',
            ('store_id', '=', store.id),
            ('store_id', '=', False),
        ])
        if not maps:
            return False
        candidate_order = {value: index for index, value in enumerate(candidates)}
        maps = maps.sorted(
            key=lambda record: (
                0 if record.store_id.id == store.id else 1,
                candidate_order.get(record.inventory_id or '', 9999),
                record.id,
            )
        )
        return maps[:1]

    def _get_price_values(self, store, item, imported_price_map=False):
        rrp_incl = round(_to_float(item.get('RRP')), 4)
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
        if imported_price_map:
            if imported_price_map.unit_price > 0:
                list_price = imported_price_map.unit_price
            if imported_price_map.rrp > 0:
                rrp_incl = imported_price_map.rrp
        return {
            'sale_price': list_price,
            'rrp_incl': rrp_incl,
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
        for raw in (_get_neto_individual_barcode(item), _get_neto_generic_barcode(item)):
            if raw and raw != '0':
                values.append(raw)
                normalized = _strip_leading_zeroes(raw)
                if normalized and normalized != '0' and normalized not in values:
                    values.append(normalized)
        return values

    def _get_barcode_match_domains(self, Product, item):
        domains = []
        individual_barcode = _get_neto_individual_barcode(item)
        generic_barcode = _get_neto_generic_barcode(item)
        for barcode in (individual_barcode, _strip_leading_zeroes(individual_barcode)):
            if barcode and barcode != '0':
                domains.append(('barcode', barcode))
        if 'reza_generic_barcode' in Product._fields:
            for barcode in (generic_barcode, _strip_leading_zeroes(generic_barcode)):
                if barcode and barcode != '0':
                    domains.append(('reza_generic_barcode', barcode))
        return list(dict.fromkeys(domains))

    def _select_barcode_match(self, products):
        if not products:
            return False, False
        active = products.filtered(lambda product: product.active)
        if len(active) == 1:
            return active, False
        if len(products) == 1:
            return products, False
        return False, True

    def _match_by_barcode_candidates(self, Product, barcode_domains):
        for field_name, barcode in barcode_domains:
            products = Product.search([(field_name, '=', barcode)])
            matched, conflict = self._select_barcode_match(products)
            if matched:
                return matched, False
            if conflict:
                _logger.info(
                    'Neto product sync: ignoring shared %s %s matched to %d product(s)',
                    field_name, barcode, len(products),
                )
        return False, False

    def _is_linked_product_compatible_with_item(self, product, item):
        sku = (item.get('SKU') or '').strip()
        sku_variants = _get_sku_variants(sku)
        default_code = (product.default_code or '').strip()
        if sku_variants and default_code and default_code not in sku_variants:
            return False
        if not product.active and _name_similarity(item.get('Name'), product.display_name) < 0.35:
            return False
        return True

    def _neto_company_domain(self, store):
        """Domain restricting product matches to shared products or the store's
        own company. Neto product IDs / SKUs are per-store, and Edbert / Liaise /
        Global share one database, so a SKU-only fallback must never bind to
        another company's product."""
        company = store.company_id
        if not company:
            return []
        return ['|', ('company_id', '=', False), ('company_id', '=', company.id)]

    def _match_stored_product_by_sku(self, store, sku):
        """Match an already-mapped Odoo product for a Neto SKU using ONLY data
        stored in Odoo (no live Neto API call). Store/company scoped. Returns an
        empty recordset when there is no unambiguous stored match."""
        Product = self.env['product.product'].sudo().with_context(active_test=False)
        sku = (sku or '').strip()
        if not sku:
            return Product.browse()
        ProductLink = self.env['neto.product.link'].sudo()
        sku_variants = _get_sku_variants(sku)
        # 1. Store-scoped Neto SKU link — authoritative mapping.
        for sku_variant in sku_variants:
            link = ProductLink.search([
                ('store_id', '=', store.id),
                ('neto_sku', '=', sku_variant),
            ], limit=1)
            if link and link.product_id:
                return link.product_id
        # 2. Company-scoped reference fallback (never cross-company).
        company_domain = self._neto_company_domain(store)
        for sku_variant in sku_variants:
            products = Product.search([('default_code', '=', sku_variant)] + company_domain)
            matched, _conflict = self._select_active_unique_match(products)
            if matched:
                return matched
        if 'x_studio_neto_reference' in Product._fields:
            for sku_variant in sku_variants:
                products = Product.search(
                    [('x_studio_neto_reference', '=', sku_variant)] + company_domain
                )
                matched, _conflict = self._select_active_unique_match(products)
                if matched:
                    return matched
        return Product.browse()

    def _match_existing_product(self, store, item):
        Product = self.env['product.product'].sudo().with_context(active_test=False)
        ProductLink = self.env['neto.product.link'].sudo()
        neto_product_id = (item.get('ID') or item.get('InventoryID') or '').strip()
        sku = (item.get('SKU') or '').strip()
        sku_variants = _get_sku_variants(sku)
        saw_ambiguous_sku_match = False
        if neto_product_id:
            link = ProductLink.search([
                ('store_id', '=', store.id),
                ('neto_product_id', '=', neto_product_id),
            ], limit=1)
            if link:
                if self._is_linked_product_compatible_with_item(link.product_id, item):
                    return link.product_id, False
                _logger.info(
                    'Neto product sync: existing link %s for store %s points to incompatible '
                    'product %s; rematching SKU=%s Neto ID=%s',
                    link.id, store.display_name, link.product_id.display_name, sku, neto_product_id,
                )
            product = Product.search([
                ('neto_store_id', '=', store.id),
                ('neto_product_id', '=', neto_product_id),
            ], limit=1)
            if product:
                return product, False
            if Product.search([('neto_product_id', '=', neto_product_id)], limit=1):
                _logger.info(
                    'Neto product sync: Neto product ID %s exists outside store %s; '
                    'not using it as a cross-store product match',
                    neto_product_id, store.display_name,
                )
            if (
                'external_api_id' in Product._fields
                and neto_product_id.isdigit()
                and Product.search([('external_api_id', '=', int(neto_product_id))], limit=1)
            ):
                _logger.info(
                    'Neto product sync: external API ID %s exists outside store %s; '
                    'not using it as a cross-store product match',
                    neto_product_id, store.display_name,
                )
        for sku_variant in sku_variants:
            link = ProductLink.search([
                ('store_id', '=', store.id),
                ('neto_sku', '=', sku_variant),
            ], limit=1)
            if link:
                if self._is_linked_product_compatible_with_item(link.product_id, item):
                    return link.product_id, False
                _logger.info(
                    'Neto product sync: existing SKU link %s for store %s points to incompatible '
                    'product %s; rematching SKU=%s',
                    link.id, store.display_name, link.product_id.display_name, sku_variant,
                )
        reference_candidates = _get_neto_reference_candidates(item)
        for sku_variant in sku_variants:
            if sku_variant not in reference_candidates:
                reference_candidates.append(sku_variant)
        company_domain = self._neto_company_domain(store)
        for reference in reference_candidates:
            products = Product.search([('default_code', '=', reference)] + company_domain)
            matched, conflict = self._select_active_unique_match(products)
            if matched:
                return matched, False
            if conflict:
                saw_ambiguous_sku_match = True
        if 'x_studio_neto_reference' in Product._fields:
            for sku_variant in sku_variants:
                products = Product.search(
                    [('x_studio_neto_reference', '=', sku_variant)] + company_domain
                )
                matched, conflict = self._select_active_unique_match(products)
                if matched:
                    return matched, False
                if conflict:
                    saw_ambiguous_sku_match = True
        barcode_domains = self._get_barcode_match_domains(Product, item)
        if barcode_domains:
            for field_name, barcode in barcode_domains:
                if Product.search([(field_name, '=', barcode)], limit=1):
                    _logger.info(
                        'Neto product sync: %s %s exists but is not used as a product match '
                        'without an exact Neto link or SKU/reference match',
                        field_name, barcode,
                    )
        if saw_ambiguous_sku_match:
            return False, True
        return False, False

    def _match_exact_stock_product(self, store, item):
        ProductLink = self.env['neto.product.link'].sudo()
        neto_product_id = (item.get('ID') or item.get('InventoryID') or '').strip()
        if neto_product_id:
            link = ProductLink.search([
                ('store_id', '=', store.id),
                ('neto_product_id', '=', neto_product_id),
            ], limit=1)
            if link:
                return link.product_id, False
        sku = (item.get('SKU') or '').strip()
        if not sku:
            return False, 'Skipped stock row without SKU'
        link = ProductLink.search([
            ('store_id', '=', store.id),
            ('neto_sku', '=', sku),
        ], limit=1)
        if link:
            return link.product_id, False
        Product = self.env['product.product'].sudo().with_context(active_test=False)
        products = Product.search([('default_code', '=', sku)])
        product, conflict = self._select_active_unique_match(products)
        if conflict:
            return False, 'Skipped stock row because exact SKU matches multiple Odoo products'
        if not product:
            return False, 'Skipped stock row because exact SKU does not exist in Odoo'
        return product, False

    def _sync_pricelist_price(self, pricelist, product, sale_price):
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

    def _get_or_create_product(self, store, item, category):
        """Every Neto item becomes its own single-variant template.

        Neto's ParentSKU / IsVariant do not describe an attribute axis: parents
        group by model, by colourway, or by nothing at all (p_header lumps 35
        unrelated display stands together), and the axis differs per store. Any
        attempt to reconstruct Odoo attributes from them produced either a
        SKU-as-attribute-value or phantom cartesian variants. They are kept as
        reference data on neto.product.link instead, and never as structure.
        """
        ProductTemplate = self.env['product.template'].sudo()
        template = ProductTemplate.create({
            'name': (item.get('Name') or item.get('SKU') or 'Neto Product').strip(),
            'categ_id': category.id,
            'company_ids': [(4, _EDBERT_COMPANY_ID), (4, store.company_id.id)],
            'neto_parent_sku': (item.get('ParentSKU') or '').strip() or False,
            'sale_ok': True,
            'purchase_ok': True,
            'active': True,
        })
        return template.product_variant_ids[:1], template

    def _get_warehouse_quantity_json(self, item):
        value = item.get('WarehouseQuantity')
        if value in (None, '', []):
            return False
        try:
            return json.dumps(value, sort_keys=True)
        except TypeError:
            return str(value)

    def _prepare_product_link_values(self, store, product, item, reason=False):
        values = {
            'product_id': product.id,
            'store_id': store.id,
            'neto_product_id': (item.get('ID') or item.get('InventoryID') or '').strip() or False,
            'neto_sku': (item.get('SKU') or '').strip() or False,
            'neto_barcode': _get_neto_individual_barcode(item) or False,
            'neto_generic_barcode': _get_neto_generic_barcode(item) or False,
            'neto_parent_sku': (item.get('ParentSKU') or '').strip() or False,
            'neto_name': (item.get('Name') or '').strip() or False,
            'neto_model': (item.get('Model') or '').strip() or False,
            'neto_brand': (item.get('Brand') or '').strip() or False,
            'is_active': self._is_neto_item_active(item),
            'available_sell_quantity': self._get_neto_available_sell_quantity(item) or 0.0,
            'warehouse_quantity_json': self._get_warehouse_quantity_json(item),
            'last_sync_date': fields.Datetime.now(),
            'last_sync_note': reason or False,
        }
        values.update(self._prepare_neto_reference_values(item))
        return values

    def _prepare_neto_reference_values(self, item):
        """Neto fields kept verbatim for reference. None of these drive Odoo
        behaviour -- they exist so a human can see what Neto actually said about
        this product without opening the Neto admin."""
        return {
            'neto_is_variant': _to_bool(item.get('IsVariant')),
            'neto_reference_number': _clean_str(item.get('ReferenceNumber')),
            'neto_subtitle': _clean_str(item.get('Subtitle')),
            'neto_categories': _dump_json(item.get('Categories')),
            'neto_default_price': _to_float(item.get('DefaultPrice')),
            'neto_rrp': _to_float(item.get('RRP')),
            'neto_cost_price': _to_float(item.get('CostPrice')),
            'neto_purchase_price': _to_float(item.get('DefaultPurchasePrice')),
            'neto_promotion_price': _to_float(item.get('PromotionPrice')),
            'neto_tax_inclusive': _to_bool(item.get('TaxInclusive')),
            'neto_price_groups_json': _dump_json(item.get('PriceGroups')),
            'neto_shipping_weight': _to_float(item.get('ShippingWeight')),
            'neto_shipping_height': _to_float(item.get('ShippingHeight')),
            'neto_shipping_width': _to_float(item.get('ShippingWidth')),
            'neto_shipping_length': _to_float(item.get('ShippingLength')),
            'neto_cubic_weight': _to_float(item.get('CubicWeight')),
            'neto_primary_supplier': _clean_str(item.get('PrimarySupplier')),
            'neto_supplier_item_code': _clean_str(item.get('SupplierItemCode')),
            'neto_description': _clean_str(item.get('Description')),
            'neto_short_description': _clean_str(item.get('ShortDescription')),
            'neto_specifications': _dump_json(item.get('ItemSpecifics')),
            'neto_search_keywords': _clean_str(item.get('SearchKeywords')),
            'neto_seo_page_title': _clean_str(item.get('SEOPageTitle')),
            'neto_seo_meta_description': _clean_str(item.get('SEOMetaDescription')),
            'neto_date_added': _clean_str(item.get('DateAdded')),
            'neto_date_updated': _clean_str(item.get('DateUpdated')),
            'neto_approved': _to_bool(item.get('Approved')),
            'neto_virtual': _to_bool(item.get('Virtual')),
            'neto_raw_json': _dump_json(item, pretty=True),
        }

    def _upsert_product_link(self, store, product, item, reason=False):
        ProductLink = self.env['neto.product.link'].sudo()
        neto_product_id = (item.get('ID') or item.get('InventoryID') or '').strip()
        sku = (item.get('SKU') or '').strip()
        link = False
        if neto_product_id:
            link = ProductLink.search([
                ('store_id', '=', store.id),
                ('neto_product_id', '=', neto_product_id),
            ], limit=1)
        if not link and sku:
            link = ProductLink.search([
                ('store_id', '=', store.id),
                ('neto_sku', '=', sku),
            ], limit=1)
        values = self._prepare_product_link_values(store, product, item, reason=reason)
        if link:
            link.write(values)
        else:
            link = ProductLink.create(values)
        return link

    def _prepare_product_write_values(self, store, item, price_values, action, reason=False, product=False):
        sku = (item.get('SKU') or '').strip()
        barcode = _get_neto_individual_barcode(item)
        generic_barcode = _get_neto_generic_barcode(item)
        neto_product_id = (item.get('ID') or item.get('InventoryID') or '').strip()
        parent_sku = (item.get('ParentSKU') or '').strip()
        values = {
            'recommended_retail_price': price_values['rrp_incl'],
            'standard_price': price_values['cost_price'],
            'neto_last_product_sync': fields.Datetime.now(),
            'neto_product_sync_state': action,
            'neto_product_sync_note': reason or False,
        }
        legacy_store = product.neto_store_id if product else False
        can_write_legacy_store_fields = not product or not legacy_store or legacy_store == store
        if can_write_legacy_store_fields:
            values.update({
                'neto_product_id': neto_product_id or False,
                'neto_store_id': store.id,
                'neto_parent_sku': parent_sku or False,
            })
            if not product or not product.barcode or legacy_store == store:
                values['barcode'] = barcode or False
        if (
            can_write_legacy_store_fields
            and 'reza_generic_barcode' in self.env['product.product']._fields
            and (
                not product
                or not getattr(product, 'reza_generic_barcode', False)
                or legacy_store == store
            )
        ):
            values['reza_generic_barcode'] = generic_barcode or False
        if can_write_legacy_store_fields and 'external_api_id' in self.env['product.product']._fields:
            values['external_api_id'] = int(neto_product_id) if neto_product_id.isdigit() else False
        # Preserve an existing Odoo internal reference on matched products.
        if not product or not product.default_code:
            values['default_code'] = sku or False
        if 'x_studio_neto_reference' in self.env['product.product']._fields:
            values['x_studio_neto_reference'] = sku or False
        return values

    def _is_barcode_conflict(self, exc):
        return isinstance(exc, ValidationError) and 'Barcode(s) already assigned' in str(exc)

    def _get_neto_available_sell_quantity(self, item):
        quantity_keys = (
            'AvailableSellQuantity',
            'AvailableQuantity',
            'QuantityAvailable',
            'QtyAvailable',
            'Available',
            'AvailableStock',
            'StockAvailable',
            'FreeStock',
            'SellableQuantity',
            'AvailableForSale',
            'QtyInStock',
            'QuantityOnHand',
            'Quantity',
            'Qty',
        )
        for key in quantity_keys:
            qty = _to_optional_float(item.get(key))
            if qty is not None:
                return qty

        warehouse_quantity = item.get('WarehouseQuantity') or []

        def _iter_warehouse_records(value):
            if isinstance(value, list):
                for entry in value:
                    yield from _iter_warehouse_records(entry)
            elif isinstance(value, dict):
                yielded_nested = False
                for nested_key in ('Warehouse', 'WarehouseQuantity'):
                    nested = value.get(nested_key)
                    if nested:
                        yielded_nested = True
                        yield from _iter_warehouse_records(nested)
                if not yielded_nested:
                    yield value

        def _extract_quantity(record):
            if not isinstance(record, dict):
                return None
            for key in quantity_keys:
                qty = _to_optional_float(record.get(key))
                if qty is not None:
                    return qty
            return None

        total = 0.0
        found = False
        for warehouse in _iter_warehouse_records(warehouse_quantity):
            quantity = _extract_quantity(warehouse)
            if quantity is None:
                continue
            total += quantity
            found = True
        return total if found else None

    def _collect_variant_parent_skus(self, items):
        parent_skus = set()
        for item in items:
            is_variant = str(item.get('IsVariant') or '').strip().lower() == 'true'
            parent_sku = (item.get('ParentSKU') or '').strip().lower()
            if is_variant and parent_sku and parent_sku != '0':
                parent_skus.add(parent_sku)
        return parent_skus

    def _is_parent_stock_item(self, item, variant_parent_skus=None):
        sku = (item.get('SKU') or '').strip().lower()
        is_variant = str(item.get('IsVariant') or '').strip().lower() == 'true'
        if is_variant:
            return False
        parent_skus = variant_parent_skus or set()
        return bool(sku and sku in parent_skus)

    def _is_neto_item_active(self, item):
        return str(item.get('IsActive') or '').strip().lower() != 'false'

    def _ensure_stockable_product(self, product):
        template = product.product_tmpl_id
        values = {}
        if 'is_storable' in template._fields and not template.is_storable:
            values['is_storable'] = True
        elif 'type' in template._fields:
            selection = template._fields['type'].selection
            if isinstance(selection, list) and any(key == 'product' for key, label in selection):
                if template.type != 'product':
                    values['type'] = 'product'
        if values:
            template.sudo().write(values)

    def _update_available_quantity_with_retry(self, store, product, location, delta, attempts=6):
        Quant = self.env['stock.quant'].sudo().with_company(store.company_id)
        product = product.with_company(store.company_id)
        for attempt in range(1, attempts + 1):
            try:
                with self.env.cr.savepoint():
                    Quant._update_available_quantity(product, location, delta)
                return True
            except pg_errors.SerializationFailure:
                if attempt >= attempts:
                    raise
                time.sleep(0.25 * attempt)
        return False

    def _sync_stock_quantity(self, store, product, item, update_neto_quantity=True, variant_parent_skus=None):
        if self._is_parent_stock_item(item, variant_parent_skus=variant_parent_skus):
            self._log_product_sync(
                store, item, 'skipped', product=product,
                reason='Skipped parent stock row; variant rows carry sellable quantity',
            )
            return False
        if not self._is_neto_item_active(item):
            qty = 0.0
        else:
            qty = self._get_neto_available_sell_quantity(item)
            if qty is None:
                self._log_product_sync(
                    store, item, 'skipped', product=product,
                    reason='Skipped active stock row because Neto did not provide an explicit quantity',
                )
                return False
        location = store.warehouse_id.lot_stock_id
        if not location:
            self._log_product_sync(store, item, 'skipped', product=product, reason='Store warehouse has no stock location')
            return False
        self._ensure_stockable_product(product)
        product = product.with_company(store.company_id)
        current_qty = product.with_context(location=location.id).qty_available
        delta = round(qty - current_qty, 4)
        if delta:
            self._update_available_quantity_with_retry(store, product, location, delta)
        if update_neto_quantity and (not product.neto_store_id or product.neto_store_id == store):
            product.sudo().write({'neto_available_sell_quantity': qty})
        link = self._upsert_product_link(store, product, item)
        link.sudo().write({
            'available_sell_quantity': qty,
            'last_sync_date': fields.Datetime.now(),
        })
        return True

    def _write_product_record(
        self, product, template, store, item, category, action, reason=False, sync_stock=True
    ):
        imported_price_map = self._get_imported_price_map(store, item)
        price_values = self._get_price_values(store, item, imported_price_map=imported_price_map)
        is_variant = str(item.get('IsVariant') or '').strip().lower() == 'true'
        if price_values['cost_price'] > 0:
            template.sudo().with_company(store.company_id).write({
                'standard_price': price_values['cost_price'],
            })
        pricelist = (
            imported_price_map.pricelist_id if imported_price_map else store.pricelist_id
        )
        template_values = {
            'categ_id': category.id,
            'active': str(item.get('IsActive') or '').strip().lower() != 'false',
        }
        if not pricelist:
            template_values['list_price'] = price_values['sale_price']
        if not is_variant:
            template_values['name'] = (item.get('Name') or item.get('SKU') or template.name).strip()
        template.sudo().write(template_values)
        self._ensure_company_on_template(template, store.company_id)
        product_values = self._prepare_product_write_values(
            store, item, price_values, action, reason=reason, product=product,
        )
        link = self._upsert_product_link(store, product, item, reason=reason)
        try:
            product.sudo().write(product_values)
        except ValidationError as exc:
            if not self._is_barcode_conflict(exc):
                raise
            barcode_value = product_values.pop('barcode', False)
            retry_reason = f'Barcode conflict on {barcode_value}; kept existing Odoo barcode'
            product_values['neto_product_sync_note'] = retry_reason
            product.sudo().write(product_values)
            link.write({'last_sync_note': retry_reason})
            self._log_product_sync(store, item, 'skipped', product=product, link=link, reason=retry_reason)
        self._sync_pricelist_price(pricelist, product, price_values['sale_price'])
        if sync_stock:
            self._sync_stock_quantity(store, product, item)
        if self._is_global_store(store):
            self._set_pricing_review_tag(template)
        return link

    def _log_product_sync(self, store, item, action, product=False, link=False, reason=False):
        values = {
            'store_id': store.id if store else False,
            'neto_sku': (item.get('SKU') or '').strip() if item else False,
            'neto_product_id': (item.get('ID') or item.get('InventoryID') or '').strip() if item else False,
            'link_id': link.id if link else False,
            'product_id': product.id if product else False,
            'action': action,
            'reason': reason or False,
        }
        self.env['neto.product.sync.log'].sudo().create(values)

    def _process_product_item(self, store, item, matched_ids, sync_stock=True):
        product, conflict = self._match_existing_product(store, item)
        if conflict:
            self._log_product_sync(store, item, 'conflict', reason='Ambiguous existing Odoo match')
            return False
        category = self._get_or_create_category(store, item)
        if product:
            template = product.product_tmpl_id
            link = self._write_product_record(
                product, template, store, item, category, 'updated', sync_stock=sync_stock
            )
            matched_ids.add(product.id)
            self._log_product_sync(store, item, 'updated', product=product, link=link)
            return product
        product, template = self._get_or_create_product(store, item, category)
        if not product:
            self._log_product_sync(store, item, 'error', reason='Unable to create Odoo product')
            return False
        link = self._write_product_record(
            product, template, store, item, category, 'created', sync_stock=sync_stock
        )
        matched_ids.add(product.id)
        self._log_product_sync(store, item, 'created', product=product, link=link)
        return product

    def _split_products(self, items):
        """Kept for API compatibility. The catalogue is flat: every Neto item is
        its own product, so there is nothing to split. Variant children used to be
        deferred to a second pass so their parent template existed first; with no
        parent templates that ordering no longer matters."""
        return list(items), []

    def _collect_active_signatures(self, items):
        signatures = {
            'neto_ids': set(),
            'skus': set(),
            'references': set(),
            'barcodes': set(),
        }
        for item in items:
            neto_id = (item.get('ID') or item.get('InventoryID') or '').strip()
            sku = (item.get('SKU') or '').strip()
            if neto_id:
                signatures['neto_ids'].add(neto_id)
            if sku:
                signatures['skus'].add(sku)
            for reference in _get_neto_reference_candidates(item):
                signatures['references'].add(reference)
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
            if product.default_code and product.default_code in signatures.get('references', set()):
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

    def _sync_product_store(self, store, include_active=True, include_inactive=False, sync_stock=False):
        if not store.api_key or not store.store_url:
            _logger.warning(
                'Neto product sync: store "%s" missing api_key or store_url — skipping.',
                store.name,
            )
            return set(), {}
        _logger.info(
            'Neto product sync [%s]: fetching products active=%s inactive=%s',
            store.name, include_active, include_inactive,
        )
        items = self._fetch_all_products(
            store, include_active=include_active, include_inactive=include_inactive
        )
        standalones, variants = self._split_products(items)
        matched_ids = set()
        write_counter = 0
        for item in standalones + variants:
            try:
                with self.env.cr.savepoint():
                    product = self._process_product_item(
                        store, item, matched_ids, sync_stock=sync_stock
                    )
            except Exception as exc:
                # Savepoint rolls back the poisoned write, so the error log below
                # runs on a healthy cursor and the rest of the store still syncs.
                _logger.exception(
                    'Neto product sync [%s]: failed on SKU %s',
                    store.name, item.get('SKU'),
                )
                self._log_product_sync(store, item, 'error', reason=str(exc))
                self.env.cr.commit()
                continue
            if product:
                write_counter += 1
                if write_counter % _PRODUCT_WRITE_BATCH_SIZE == 0:
                    self.env.cr.commit()
        store.sudo().write({'last_product_sync_date': fields.Datetime.now()})
        _logger.info(
            'Neto product sync [%s]: completed — %d item(s), %d matched/created.',
            store.name, len(items), len(matched_ids),
        )
        return matched_ids, self._collect_active_signatures(items)

    def _sync_product_stock_store(self, store):
        self = self.with_context(
            mail_notrack=True,
            mail_create_nolog=True,
            mail_create_nosubscribe=True,
            mail_post_autofollow=False,
            mail_auto_subscribe_no_notify=True,
            mail_notify_force_send=False,
            mail_notify_noemail=True,
            tracking_disable=True,
            neto_skip_chatter_post=True,
        )
        store_name = store.name
        if not store.api_key or not store.store_url:
            _logger.warning(
                'Neto product stock sync: store "%s" missing api_key or store_url — skipping.',
                store_name,
            )
            return 0
        _logger.info('Neto product stock sync [%s]: fetching active and inactive products', store_name)
        items = self._fetch_all_products(store, include_active=True, include_inactive=True)
        variant_parent_skus = self._collect_variant_parent_skus(items)
        item_skus = {
            (item.get('SKU') or '').strip()
            for item in items
            if (item.get('SKU') or '').strip()
        }
        updated = 0
        skipped = 0
        seen_product_ids = set()
        for item in items:
            try:
                with self.env.cr.savepoint():
                    product, reason = self._match_exact_stock_product(store, item)
                    if not product:
                        skipped += 1
                        self._log_product_sync(store, item, 'skipped', reason=reason)
                        continue
                    if product.id in seen_product_ids:
                        skipped += 1
                        self._log_product_sync(
                            store, item, 'skipped', product=product,
                            reason='Duplicate Neto stock row matched to this Odoo product in the same run',
                        )
                        continue
                    seen_product_ids.add(product.id)
                    if self._sync_stock_quantity(
                        store, product, item,
                        update_neto_quantity=False,
                        variant_parent_skus=variant_parent_skus,
                    ):
                        updated += 1
            except Exception as exc:
                _logger.exception(
                    'Neto product stock sync [%s]: failed on SKU %s — %s',
                    store_name, item.get('SKU'), exc,
                )
        updated += self._zero_absent_store_stock(store, item_skus)
        self.env.cr.commit()
        _logger.info(
            'Neto product stock sync [%s]: completed — %d updated, %d skipped.',
            store_name, updated, skipped,
        )
        return updated

    def _zero_absent_store_stock(self, store, item_skus):
        location = store.warehouse_id.lot_stock_id
        if not location:
            return 0
        quants = self.env['stock.quant'].sudo().with_company(store.company_id).search([
            ('location_id', 'child_of', location.id),
            ('quantity', '!=', 0),
        ])
        updated = 0
        seen_product_ids = set()
        for product in quants.mapped('product_id'):
            if product.id in seen_product_ids:
                continue
            seen_product_ids.add(product.id)
            sku = (product.default_code or '').strip()
            if not sku or sku in item_skus:
                continue
            if not self._is_neto_linked_product(product):
                continue
            current_qty = product.with_company(store.company_id).with_context(
                location=location.id
            ).qty_available
            if not current_qty:
                continue
            try:
                self._update_available_quantity_with_retry(store, product, location, -current_qty)
            except pg_errors.SerializationFailure as exc:
                _logger.warning(
                    'Neto product stock sync [%s]: skipped zeroing SKU %s after stock quant '
                    'lock retries — %s',
                    store.name, sku, exc,
                )
                self._log_product_sync(
                    store, {'SKU': sku, 'ID': product.neto_product_id or ''}, 'skipped',
                    product=product,
                    reason='Skipped absent-store zeroing because stock quant stayed locked',
                )
                continue
            self._log_product_sync(
                store, {'SKU': sku, 'ID': product.neto_product_id or ''}, 'updated',
                product=product,
                reason='Zeroed store warehouse stock because SKU is absent from this Neto store',
            )
            updated += 1
        return updated

    def _is_neto_linked_product(self, product):
        if self.env['neto.product.link'].sudo().search([('product_id', '=', product.id)], limit=1):
            return True
        if product.neto_product_id or product.neto_parent_sku or product.neto_store_id:
            return True
        if 'x_studio_neto_reference' in product._fields and product.x_studio_neto_reference:
            return True
        return False

    def backfill_legacy_product_links(self):
        Product = self.env['product.product'].sudo().with_context(active_test=False)
        products = Product.search([
            ('neto_store_id', '!=', False),
            ('neto_product_id', '!=', False),
        ])
        updated = 0
        for product in products:
            item = {
                'ID': product.neto_product_id or '',
                'SKU': product.default_code or '',
                'UPC': product.barcode or '',
                'UPC1': getattr(product, 'reza_generic_barcode', False) or '',
                'ParentSKU': product.neto_parent_sku or '',
                'Name': product.display_name or product.name or '',
                'AvailableSellQuantity': product.neto_available_sell_quantity,
                'IsActive': 'True' if product.active else 'False',
            }
            link = self._upsert_product_link(
                product.neto_store_id,
                product,
                item,
                reason='Backfilled from legacy product Neto fields',
            )
            if product.neto_last_product_sync:
                link.write({'last_sync_date': product.neto_last_product_sync})
            updated += 1
        return updated

    def run_product_stock_sync(self, store_ids=None):
        domain = [('active', '=', True)]
        if store_ids:
            domain.append(('id', 'in', store_ids))
        stores = self.env['neto.store'].sudo().search(domain)
        if not stores:
            _logger.warning('Neto product stock sync: no active stores configured — aborting.')
            return
        for store in stores:
            store_name = store.name
            try:
                self._sync_product_stock_store(store)
            except Exception as exc:
                _logger.exception(
                    'Neto product stock sync: store "%s" failed — %s',
                    store_name, exc,
                )

    def run_product_sync(
        self, store_ids=None, include_active=True, include_inactive=False, sync_stock=False
    ):
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
                matched_ids, signatures = self._sync_product_store(
                    store,
                    include_active=include_active,
                    include_inactive=include_inactive,
                    sync_stock=sync_stock,
                )
                all_matched_ids.update(matched_ids)
                signatures_by_store[store.id] = signatures
            except Exception as exc:
                _logger.exception(
                    'Neto product sync: store "%s" failed — %s',
                    store.name, exc,
                )
        if include_active:
            self._log_unmatched_products(stores, all_matched_ids, signatures_by_store)
