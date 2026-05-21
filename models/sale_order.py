# -*- coding: utf-8 -*-
from odoo import models, fields


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
