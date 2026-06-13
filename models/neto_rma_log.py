# -*- coding: utf-8 -*-
from odoo import models, fields


class NetoRmaLog(models.Model):
    _name = "neto.rma.log"
    _description = "Neto RMA Sync Log"
    _order = "sync_date desc"

    neto_rma_id = fields.Char(string="Neto RMA ID", required=True, index=True)
    neto_invoice_number = fields.Char(string="Neto Invoice Number")
    neto_username = fields.Char(string="Neto Username")
    neto_rma_status = fields.Char(string="Neto RMA Status")
    neto_refund_total = fields.Float(string="Refund Total", digits=(16, 2))
    state = fields.Selection(
        selection=[
            ("success", "Success"),
            ("partial", "Partial"),
            ("skipped", "Skipped"),
            ("error", "Error"),
        ],
        string="State", required=True, index=True,
    )
    skip_reason = fields.Char(string="Skip Reason")
    error_message = fields.Text(string="Error Message")
    store_id = fields.Many2one("neto.store", string="Store", ondelete="set null", index=True)
    credit_note_id = fields.Many2one("account.move", string="Credit Note", ondelete="set null")
    partner_id = fields.Many2one("res.partner", string="Partner", ondelete="set null")
    sync_date = fields.Datetime(string="Sync Date", default=fields.Datetime.now, readonly=True)

    def upsert_for_rma(self, store, neto_rma_id, values):
        """Keep one current sync result per store/RMA pair."""
        log = self.search([
            ("store_id", "=", store.id),
            ("neto_rma_id", "=", neto_rma_id),
        ], order="id desc", limit=1)
        values = {
            **values,
            "store_id": store.id,
            "neto_rma_id": neto_rma_id,
            "sync_date": fields.Datetime.now(),
        }
        if log:
            log.write(values)
            return log
        return self.create(values)
