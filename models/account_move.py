# -*- coding: utf-8 -*-
from odoo import models, fields


class AccountMove(models.Model):
    _inherit = "account.move"

    neto_rma_id = fields.Char(string="Neto RMA ID", index=True, copy=False)
    neto_rma_store_id = fields.Many2one(
        "neto.store", string="Neto RMA Store", index=True, copy=False, ondelete="restrict",
    )
    neto_rma_status = fields.Char(string="Neto RMA Status", copy=False, readonly=True)

    _neto_rma_store_id_uniq = models.Constraint(
        "unique(neto_rma_store_id, neto_rma_id)",
        "This Neto RMA has already been synced for this store.",
    )


class AccountPayment(models.Model):
    _inherit = "account.payment"

    neto_rma_id = fields.Char(string="Neto RMA ID", index=True, copy=False)
    neto_rma_store_id = fields.Many2one(
        "neto.store", string="Neto RMA Store", index=True, copy=False, ondelete="restrict",
    )
    neto_rma_refund_key = fields.Char(string="Neto RMA Refund Key", index=True, copy=False)

    _neto_rma_refund_key_uniq = models.Constraint(
        "unique(neto_rma_store_id, neto_rma_refund_key)",
        "This Neto RMA refund has already been synced for this store.",
    )
