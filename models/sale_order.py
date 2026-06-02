# -*- coding: utf-8 -*-
from odoo import _, fields, models
from odoo.exceptions import UserError


_NETO_SHIPPING_SKU = "NETO_SHIPPING"
_NETO_SURCHARGE_SKU = "NETO-SURCHARGE"


# ---------------------------------------------------------------------------
# Neto status → badge decoration mapping
# ---------------------------------------------------------------------------
_STATUS_DECORATION = {
    # green
    'Pick':             'success',
    'Pack':             'success',
    # blue
    'Dispatched':       'info',
    'New Backorder':    'info',
    # orange
    'New':              'warning',
    'Pending':          'warning',
    'On Hold':          'warning',
    'Pickup':           'warning',
    'Pending Pickup':   'warning',
    'Partial Dispatch': 'warning',
    # red
    'Cancelled':        'danger',
    'Declined':         'danger',
    'Refunded':         'danger',
}


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    neto_order_id = fields.Char(
        string='Neto Order ID', index=True, copy=False,
    )
    neto_order_status = fields.Char(
        string='Neto Status', copy=False, readonly=True,
    )
    neto_consignment_number = fields.Char(
        string='Neto Consignment Number',
        copy=False,
        readonly=True,
        help='Tracking / consignment number returned by Neto order lines.',
    )
    neto_date_paid = fields.Datetime(
        string='Neto Date Paid', copy=False, readonly=True,
    )
    neto_payment_method = fields.Char(
        string='Neto Payment Method', copy=False, readonly=True,
    )
    neto_internal = fields.Boolean(
        string='Neto Internal',
        copy=False,
        default=False,
        help='Flagged True for zero-value internal transfers and '
             'BrightEyes internal replenishment orders. '
             'Fully synced but can be filtered out of sales reports.',
    )
    neto_history_import = fields.Boolean(
        string='Neto History Import',
        copy=False,
        readonly=True,
        help='True when this Neto order was imported in history mode as a quotation '
             'to preserve reporting/outstanding balances.',
    )
    neto_internal_label = fields.Char(
        string='Internal Label',
        compute='_compute_neto_internal_label',
        store=False,
        help='Returns "Internal" when neto_internal is True, else empty string. '
             'Used for the header badge (boolean fields cannot use widget=badge in Odoo 18).',
    )
    neto_status_decoration = fields.Char(
        string='Neto Status Decoration',
        compute='_compute_neto_status_decoration',
        store=False,
        help='Bootstrap decoration class derived from neto_order_status for badge colouring.',
    )

    def _compute_neto_internal_label(self):
        for order in self:
            order.neto_internal_label = 'Internal' if order.neto_internal else ''

    def _compute_neto_status_decoration(self):
        for order in self:
            order.neto_status_decoration = _STATUS_DECORATION.get(
                order.neto_order_status or '', 'muted'
            )

    def action_print_neto_history_tax_invoice(self):
        self.ensure_one()
        if not self._is_neto_history_tax_invoice_allowed():
            raise UserError(_(
                "Historical Tax Invoice can only be printed for Neto history "
                "imports that have a Neto Order ID."
            ))
        return self.env.ref(
            "Reza_neto_connector.action_report_neto_history_tax_invoice"
        ).report_action(self)

    def _is_neto_history_tax_invoice_allowed(self):
        self.ensure_one()
        return bool(self.neto_history_import and self.neto_order_id)

    def _get_neto_history_product_lines(self):
        self.ensure_one()
        return self.order_line.filtered(
            lambda line: (
                not line.display_type
                and line.state != "cancel"
                and not self._is_neto_history_shipping_line(line)
                and not self._is_neto_history_surcharge_line(line)
            )
        )

    def _get_neto_history_shipping_lines(self):
        self.ensure_one()
        return self.order_line.filtered(self._is_neto_history_shipping_line)

    def _get_neto_history_surcharge_lines(self):
        self.ensure_one()
        return self.order_line.filtered(self._is_neto_history_surcharge_line)

    def _is_neto_history_shipping_line(self, line):
        sku = (line.product_id.default_code or "").strip()
        return bool(
            not line.display_type
            and line.state != "cancel"
            and (getattr(line, "is_delivery", False) or sku == _NETO_SHIPPING_SKU)
        )

    def _is_neto_history_surcharge_line(self, line):
        sku = (line.product_id.default_code or "").strip()
        return bool(
            not line.display_type
            and line.state != "cancel"
            and (
                getattr(line, "is_reza_fuel_surcharge_line", False)
                or sku == _NETO_SURCHARGE_SKU
            )
        )

    def _format_neto_history_money(self, amount):
        self.ensure_one()
        return "$%.2f" % (amount or 0.0)

    def _get_neto_history_bank_deposit_label(self):
        self.ensure_one()
        bank = self.company_id.partner_id.bank_ids[:1]
        bank_name = bank.bank_id.name if bank and bank.bank_id else ""
        if bank_name:
            return "Direct Bank Deposit into %s" % bank_name
        return "Direct Bank Deposit"

    def _get_neto_history_bank_account(self):
        self.ensure_one()
        company_key = "%s %s" % (
            self.company_id.name or "",
            self.company_id.vat or "",
        )
        if "44137818234" in company_key or "GLOBAL EYEWEAR" in company_key.upper():
            return "084 004 - 895 054 818"
        if "70115521821" in company_key or "LIAISE" in company_key.upper():
            return "084 705 - 836 921 205"
        bank = self.company_id.partner_id.bank_ids[:1]
        return bank.acc_number if bank else ""

    def _get_neto_history_line_sku(self, line):
        product = line.product_id
        if not product:
            return ""
        link = self._get_neto_history_product_link(product)
        barcode_rule = self._get_neto_history_invoice_barcode_rule()
        if barcode_rule == "generic":
            return (
                (link.neto_generic_barcode if link else "")
                or (link.neto_barcode if link else "")
                or self._get_neto_history_product_barcode(product, "reza_generic_barcode")
                or self._get_neto_history_product_barcode(product, "barcode")
                or product.default_code
                or product.neto_product_id
                or (link.neto_sku if link else "")
                or (link.neto_product_id if link else "")
                or ""
            )
        return (
            (link.neto_barcode if link else "")
            or self._get_neto_history_product_barcode(product, "barcode")
            or self._get_neto_history_product_barcode(product, "reza_generic_barcode")
            or product.default_code
            or product.neto_product_id
            or (link.neto_sku if link else "")
            or (link.neto_product_id if link else "")
            or ""
        )

    def _get_neto_history_product_link(self, product):
        self.ensure_one()
        if not product:
            return self.env["neto.product.link"]
        Store = self.env["neto.store"].sudo()
        Link = self.env["neto.product.link"].sudo()
        store = Store.search([("company_id", "=", self.company_id.id)], limit=1)
        if store:
            link = Link.search([
                ("product_id", "=", product.id),
                ("store_id", "=", store.id),
            ], limit=1)
            if link:
                return link
        return Link.search([
            ("product_id", "=", product.id),
            ("company_id", "=", self.company_id.id),
        ], limit=1)

    def _get_neto_history_invoice_barcode_rule(self):
        self.ensure_one()
        partners = (
            self.partner_invoice_id
            | self.partner_id
            | self.partner_invoice_id.commercial_partner_id
            | self.partner_id.commercial_partner_id
        )
        for partner in partners.filtered(bool):
            rule = getattr(
                partner.sudo().with_company(self.company_id),
                "reza_invoice_barcode_rule",
                False,
            )
            if rule:
                return rule
        return "individual"

    def _get_neto_history_product_barcode(self, product, field_name):
        product = product.sudo()
        return (
            getattr(product, field_name, False)
            or getattr(product.product_tmpl_id.sudo(), field_name, False)
            or ""
        )

    def _get_neto_history_line_description(self, line):
        product = line.product_id
        product_description = product.with_context(
            display_default_code=False
        ).display_name if product else ""
        line_description = (line.name or "").strip()
        if (
            product_description
            and (
                not line_description
                or line_description == (product.name or "").strip()
            )
        ):
            return product_description
        return line_description or product_description

    def _get_neto_history_line_rrp(self, line):
        return (
            getattr(line, "recommended_retail_price", 0.0)
            or getattr(line.product_id, "recommended_retail_price", 0.0)
            or 0.0
        )

    def _get_neto_history_sales_subtotal(self):
        self.ensure_one()
        return sum(self._get_neto_history_product_lines().mapped("price_subtotal"))

    def _get_neto_history_shipping_subtotal(self):
        self.ensure_one()
        return sum(self._get_neto_history_shipping_lines().mapped("price_subtotal"))

    def _get_neto_history_surcharge_subtotal(self):
        self.ensure_one()
        return sum(self._get_neto_history_surcharge_lines().mapped("price_subtotal"))

    def _get_neto_history_total_quantity(self):
        self.ensure_one()
        return int(round(sum(self._get_neto_history_product_lines().mapped("product_uom_qty"))))

    def _get_neto_history_payments(self):
        self.ensure_one()
        Payment = self.env["neto.payment"].sudo()
        payments = Payment.search([("sale_order_id", "=", self.id)])
        if not payments and self.neto_order_id:
            payments = Payment.search([
                ("neto_order_id", "=", self.neto_order_id),
                ("company_id", "=", self.company_id.id),
            ])
        return payments

    def _get_neto_history_amount_paid(self):
        self.ensure_one()
        return sum(self._get_neto_history_payments().mapped("amount_paid"))

    def _get_neto_history_amount_owed(self):
        self.ensure_one()
        return self.amount_total - self._get_neto_history_amount_paid()
