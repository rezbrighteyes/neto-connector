# -*- coding: utf-8 -*-
import logging
from datetime import timedelta, timezone

from odoo import _, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

_STALE_RUNNING_MINUTES = 30


class NetoHistoryImportJob(models.Model):
    _name = 'neto.history.import.job'
    _description = 'Neto History Import Job'
    _order = 'id desc'

    name = fields.Char(required=True, copy=False, default='New')
    store_id = fields.Many2one(
        'neto.store',
        string='Store',
        required=True,
        index=True,
        ondelete='restrict',
    )
    date_from = fields.Datetime(required=True, index=True)
    date_to = fields.Datetime(required=True, index=True)
    import_orders = fields.Boolean(default=True)
    import_payments = fields.Boolean(default=True)
    import_rmas = fields.Boolean(string='Import RMAs', default=False)
    import_as_history = fields.Boolean(default=True)
    state = fields.Selection(
        [
            ('pending', 'Pending'),
            ('running', 'Running'),
            ('done', 'Done'),
            ('cancelled', 'Cancelled'),
            ('error', 'Error'),
        ],
        default='pending',
        required=True,
        index=True,
    )
    started_at = fields.Datetime(readonly=True)
    finished_at = fields.Datetime(readonly=True)
    orders_before = fields.Integer(readonly=True)
    orders_after = fields.Integer(readonly=True)
    payments_before = fields.Integer(readonly=True)
    payments_after = fields.Integer(readonly=True)
    payments_processed = fields.Integer(readonly=True)
    payments_relinked = fields.Integer(readonly=True)
    rmas_before = fields.Integer(readonly=True)
    rmas_after = fields.Integer(readonly=True)
    rmas_processed = fields.Integer(readonly=True)
    result_message = fields.Text(readonly=True)
    error_message = fields.Text(readonly=True)

    def _order_count_domain(self):
        return [
            ('neto_order_id', '!=', False),
            ('company_id', '=', self.store_id.company_id.id),
        ]

    def action_queue(self):
        self.write({
            'state': 'pending',
            'started_at': False,
            'finished_at': False,
            'error_message': False,
        })

    def action_cancel(self):
        jobs = self.filtered(lambda job: job.state in ('pending', 'running'))
        jobs.write({
            'state': 'cancelled',
            'finished_at': fields.Datetime.now(),
            'error_message': _('Stopped manually.'),
        })
        return True

    def action_cancel_pending_queue(self):
        jobs = self.sudo().search([('state', 'in', ('pending', 'running'))])
        count = len(jobs)
        jobs.action_cancel()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Neto History Import'),
                'message': _('%s queued/running job(s) were stopped.') % count,
                'type': 'warning',
                'sticky': False,
            },
        }

    def action_delete(self):
        self.unlink()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'neto.history.import.job',
            'view_mode': 'list,form',
            'target': 'current',
            'name': _('History Import Jobs'),
        }

    def unlink(self):
        running = self.filtered(lambda job: job.state == 'running')
        if running:
            raise UserError(_('Stop running history import jobs before deleting them.'))
        return super().unlink()

    def action_process_now(self):
        for job in self:
            job._process_job()
        return True

    def _cancel_requested(self):
        self.invalidate_recordset(['state'])
        self.read(['state'])
        return self.state == 'cancelled'

    def _finish_cancelled(self):
        self.write({
            'state': 'cancelled',
            'finished_at': fields.Datetime.now(),
            'result_message': _('Stopped manually before the remaining queued work was processed.'),
        })
        self.env.cr.commit()

    def _process_job(self):
        self.ensure_one()
        if self.state == 'cancelled':
            return

        connector = self.env['neto.connector'].sudo()
        SaleOrder = self.env['sale.order'].sudo()
        Payment = self.env['neto.payment'].sudo()
        RmaLog = self.env['neto.rma.log'].sudo()

        self.write({
            'state': 'running',
            'started_at': fields.Datetime.now(),
            'error_message': False,
        })
        self.env.cr.commit()

        try:
            date_from = self.date_from.replace(tzinfo=timezone.utc)
            date_to = self.date_to.replace(tzinfo=timezone.utc)
            previous_last_sync_date = self.store_id.last_sync_date
            previous_last_payment_sync_date = self.store_id.last_payment_sync_date
            previous_last_rma_sync_date = self.store_id.last_rma_sync_date
            vals = {
                'orders_before': SaleOrder.search_count(self._order_count_domain()),
                'payments_before': Payment.search_count([('store_id', '=', self.store_id.id)]),
                'rmas_before': RmaLog.search_count([('store_id', '=', self.store_id.id)]),
            }

            if self.import_orders:
                connector._sync_store(
                    self.store_id,
                    since_dt=date_from,
                    until_dt=date_to,
                    import_as_history=self.import_as_history,
                    should_stop=self._cancel_requested,
                    update_cursor=False,
                )

            if self._cancel_requested():
                self._finish_cancelled()
                return

            payments_processed = 0
            if self.import_payments:
                payments_processed = connector.sync_payments(
                    self.store_id,
                    date_from,
                    date_to,
                )
                self.store_id.sudo().write({
                    'last_payment_sync_date': previous_last_payment_sync_date,
                })

            if self._cancel_requested():
                self._finish_cancelled()
                return

            if self.import_rmas:
                rma_complete = connector._sync_rmas(
                    self.store_id,
                    date_from,
                    until_dt=date_to,
                    should_stop=self._cancel_requested,
                )
                if self._cancel_requested():
                    self._finish_cancelled()
                    return
                if not rma_complete:
                    raise UserError(_('RMA fetch was incomplete. The job can be queued again safely.'))
                self.store_id.sudo().write({
                    'last_rma_sync_date': previous_last_rma_sync_date,
                })

            if self._cancel_requested():
                self._finish_cancelled()
                return

            payments_relinked = connector._relink_orphan_payments(self.store_id)

            orders_after = SaleOrder.search_count(self._order_count_domain())
            payments_after = Payment.search_count([('store_id', '=', self.store_id.id)])
            rmas_after = RmaLog.search_count([('store_id', '=', self.store_id.id)])
            rmas_processed = RmaLog.search_count([
                ('store_id', '=', self.store_id.id),
                ('sync_date', '>=', self.started_at),
            ]) if self.import_rmas else 0
            vals.update({
                'state': 'done',
                'finished_at': fields.Datetime.now(),
                'orders_after': orders_after,
                'payments_after': payments_after,
                'payments_processed': payments_processed,
                'payments_relinked': payments_relinked,
                'rmas_after': rmas_after,
                'rmas_processed': rmas_processed,
                'result_message': _(
                    'Orders: %(orders_before)s -> %(orders_after)s. '
                    'Payments: %(payments_before)s -> %(payments_after)s. '
                    'Payment rows processed: %(payments_processed)s. '
                    'Orphan payments relinked: %(payments_relinked)s. '
                    'RMAs: %(rmas_before)s -> %(rmas_after)s. '
                    'RMA rows processed: %(rmas_processed)s.'
                ) % {
                    'orders_before': vals['orders_before'],
                    'orders_after': orders_after,
                    'payments_before': vals['payments_before'],
                    'payments_after': payments_after,
                    'payments_processed': payments_processed,
                    'payments_relinked': payments_relinked,
                    'rmas_before': vals['rmas_before'],
                    'rmas_after': rmas_after,
                    'rmas_processed': rmas_processed,
                },
            })
            self.write(vals)
            self.env.cr.commit()
        except Exception as exc:
            _logger.exception('Neto history import job %s failed', self.id)
            restore_vals = {}
            if 'previous_last_sync_date' in locals():
                restore_vals['last_sync_date'] = previous_last_sync_date
            if 'previous_last_payment_sync_date' in locals():
                restore_vals['last_payment_sync_date'] = previous_last_payment_sync_date
            if 'previous_last_rma_sync_date' in locals():
                restore_vals['last_rma_sync_date'] = previous_last_rma_sync_date
            if restore_vals:
                self.store_id.sudo().write(restore_vals)
            self.write({
                'state': 'error',
                'finished_at': fields.Datetime.now(),
                'error_message': str(exc),
            })
            self.env.cr.commit()

    def _requeue_stale_running_jobs(self):
        stale_before = fields.Datetime.now() - timedelta(minutes=_STALE_RUNNING_MINUTES)
        stale_jobs = self.sudo().search([
            ('state', '=', 'running'),
            ('started_at', '!=', False),
            ('started_at', '<', stale_before),
        ])
        if stale_jobs:
            _logger.warning(
                'Neto history import: requeueing %d stale running job(s)',
                len(stale_jobs),
            )
            stale_jobs.write({
                'state': 'pending',
                'error_message': _(
                    'Previous cron run stopped before this job completed. '
                    'Requeued automatically.'
                ),
            })
            self.env.cr.commit()
        return len(stale_jobs)

    def cron_process_pending_history_import_jobs(self, limit=10):
        self._requeue_stale_running_jobs()
        jobs = self.sudo().search([('state', '=', 'pending')], order='id', limit=limit)
        for job in jobs:
            job._process_job()
        return True
