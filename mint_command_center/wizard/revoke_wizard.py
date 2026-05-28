import json
import logging

from odoo import api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


SCOPE_SELECTION = [
    ('single_day', 'Single Day'),
    ('from_date_forward', 'From Date Forward'),
    ('all_future_with_requeue', 'All Future + Re-queue Submission'),
    ('all_instances', 'All Instances'),
]


class RevokeDealWizard(models.TransientModel):
    _name = 'mint.deal.revoke.wizard'
    _description = 'Revoke a PTL deal from one or many days, with audit'

    deal_id = fields.Many2one(
        'mint.ptl.deal',
        string='PTL Deal',
        required=True,
    )
    submission_id = fields.Many2one(
        'mint.deal.submission',
        string='Source Submission',
        compute='_compute_submission_id',
        store=False,
    )
    scope = fields.Selection(
        selection=SCOPE_SELECTION,
        string='Revoke Scope',
        required=True,
        default='from_date_forward',
        help='single_day: only one specified day.\n'
             'from_date_forward: every day on or after the from_date.\n'
             'all_future_with_requeue: every day from today forward AND clones the source submission back to the queue.\n'
             'all_instances: every day this deal is linked to, past and future.',
    )
    target_day_id = fields.Many2one(
        'mint.ptl.day',
        string='Target Day (single_day scope)',
        help='Required when scope=single_day.',
    )
    from_date = fields.Date(
        string='From Date (from_date_forward scope)',
        default=fields.Date.context_today,
        help='Required when scope=from_date_forward.',
    )
    reason = fields.Text(
        string='Reason',
        required=True,
        help='Required. Permanent audit-log entry — be specific. e.g.: '
             '"Conflict with WTF May campaign; will re-plot after registry "'
             'cleanup."',
    )
    affected_day_ids = fields.Many2many(
        'mint.ptl.day',
        compute='_compute_affected',
        string='Affected Days',
        readonly=True,
    )
    affected_count = fields.Integer(
        compute='_compute_affected',
        string='# Affected Days',
        readonly=True,
    )

    # ─── Computes ────────────────────────────────────────────────────────

    @api.depends('deal_id')
    def _compute_submission_id(self):
        Submission = self.env['mint.deal.submission']
        for wiz in self:
            wiz.submission_id = Submission.search(
                [('deal_id', '=', wiz.deal_id.id)], limit=1
            ) if wiz.deal_id else False

    @api.depends('deal_id', 'scope', 'target_day_id', 'from_date')
    def _compute_affected(self):
        for wiz in self:
            days = wiz._resolve_affected_days()
            wiz.affected_day_ids = [(6, 0, days.ids)]
            wiz.affected_count = len(days)

    # ─── Scope resolver ──────────────────────────────────────────────────

    def _resolve_affected_days(self):
        """Return the recordset of mint.ptl.day records this revoke targets."""
        self.ensure_one()
        if not self.deal_id:
            return self.env['mint.ptl.day']
        Day = self.env['mint.ptl.day']
        base_domain = [('deal_ids', 'in', self.deal_id.id)]

        if self.scope == 'single_day':
            return self.target_day_id if self.target_day_id else Day
        if self.scope == 'from_date_forward':
            if not self.from_date:
                return Day
            return Day.search(base_domain + [('date', '>=', self.from_date)])
        if self.scope == 'all_future_with_requeue':
            today = fields.Date.context_today(self)
            return Day.search(base_domain + [('date', '>=', today)])
        if self.scope == 'all_instances':
            return Day.search(base_domain)
        return Day

    # ─── Action ──────────────────────────────────────────────────────────

    def action_apply(self):
        self.ensure_one()
        if not self.reason or not self.reason.strip():
            raise UserError("Reason is required.")
        if self.scope == 'single_day' and not self.target_day_id:
            raise UserError("scope=single_day requires a Target Day.")
        if self.scope == 'from_date_forward' and not self.from_date:
            raise UserError("scope=from_date_forward requires a From Date.")

        deal = self.deal_id
        days = self._resolve_affected_days()
        if not days:
            raise UserError(
                "No PTL days match this scope — nothing to revoke. "
                "Did you pick the right scope/from_date?"
            )

        # 1. Unlink the deal from each affected day (m2m remove).
        for day in days:
            day.write({'deal_ids': [(3, deal.id)]})

        # 2. Rescope or deactivate the linked mint.discount(s). A multi-option
        #    deal has one discount per option (discount_ids); rescope/deactivate
        #    all of them so a revoke doesn't leave secondary options serving.
        affected_discount_ids = []
        if deal.discount_ids:
            remaining_days = self.env['mint.ptl.day'].search([
                ('deal_ids', 'in', deal.id),
                ('state', '=', 'published'),
            ])
            for discount in deal.discount_ids:
                if remaining_days:
                    dates = remaining_days.mapped('date')
                    discount.write({
                        'is_published': True,
                        'valid_from': min(dates),
                        'valid_until': max(dates),
                    })
                    self.env['mint.discount']._recompute_day_booleans(discount)
                else:
                    discount.write({'is_published': False})
                affected_discount_ids.append(discount.id)

            # Push the updated discount(s) to mintinvsvc so Redis catches up.
            # Reuse the existing day-level helper via any one of the affected
            # days; if all days are gone (all_instances on a never-other-day
            # deal), fall back to a fresh ptl.day record so the helper can
            # resolve store_uuid_map.
            try:
                src_day = days[0] if days else self.env['mint.ptl.day'].search([], limit=1)
                if src_day:
                    src_day._push_discounts_to_redis(affected_discount_ids)
            except Exception as e:
                _logger.warning('Revoke: Redis push failed for discount %s: %s', discount.id, e)

        # 3. Optional submission re-queue (only when explicitly scoped).
        requeued = False
        if self.scope == 'all_future_with_requeue' and self.submission_id:
            sub = self.submission_id
            sub.write({'state': 'new', 'deal_id': False})
            sub.message_post(
                body=(
                    f"<b>Re-queued by revoke ({self.scope})</b> from deal "
                    f"\"{deal.name}\". Reason: {self.reason}"
                ),
                message_type='comment',
            )
            requeued = True

        # 4. Stamp the deal + write audit log.
        deal.write({
            'previously_revoked': True,
            'revoked_at': fields.Datetime.now(),
            'revoked_by': self.env.uid,
            'last_revoke_scope': self.scope,
        })
        deal.message_post(
            body=(
                f"<b>Revoke applied — scope: {self.scope}</b><br/>"
                f"Affected days: {len(days)} "
                f"(IDs: {', '.join(str(d.id) for d in days[:20])}"
                f"{'…' if len(days) > 20 else ''})<br/>"
                f"<b>Reason:</b> {self.reason}"
            ),
            message_type='comment',
        )

        self.env['mint.deal.revocation.audit.log'].sudo().create({
            'deal_id': deal.id,
            'submission_id': self.submission_id.id if self.submission_id else False,
            'scope': self.scope,
            'from_date': self.from_date if self.scope == 'from_date_forward' else False,
            'affected_day_ids_json': json.dumps(days.ids),
            'affected_discount_ids_json': json.dumps(affected_discount_ids),
            'requeued_submission': requeued,
            'user_id': self.env.uid,
            'reason': self.reason,
        })

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Revoke applied',
                'message': (
                    f"{len(days)} day(s) unlinked from \"{deal.name}\". "
                    f"{'Submission re-queued. ' if requeued else ''}"
                    f"Audit log written."
                ),
                'type': 'success',
                'sticky': False,
            },
        }
