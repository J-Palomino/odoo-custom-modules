import logging
from datetime import timedelta

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class MintDiscountPTL(models.Model):
    """Extend mint.discount with PTL-specific fields and lifecycle cron.

    The single Boolean `is_active` defined in mint_api_v2 is overridden here
    as a stored compute. Marketing flips `is_published`; the compute folds
    that with the valid window + day-of-week + start/end_time fields to
    yield `is_active` (which the REST API, POS, and Redis push consume).
    """
    _inherit = 'mint.discount'

    # Day-of-week availability (computed from PTL day associations)
    monday = fields.Boolean(string="Monday", default=False)
    tuesday = fields.Boolean(string="Tuesday", default=False)
    wednesday = fields.Boolean(string="Wednesday", default=False)
    thursday = fields.Boolean(string="Thursday", default=False)
    friday = fields.Boolean(string="Friday", default=False)
    saturday = fields.Boolean(string="Saturday", default=False)
    sunday = fields.Boolean(string="Sunday", default=False)

    # Source tracking
    source = fields.Selection([
        ('ptl', 'PTL'),
        ('dutchie', 'Dutchie POS'),
        ('manual', 'Manual'),
    ], string="Source", default='manual')

    # Link to PTL deal template
    ptl_deal_id = fields.Many2one(
        'mint.ptl.deal',
        string="PTL Deal",
        ondelete='set null',
    )

    # ── Override is_active as stored compute ─────────────────────────────
    is_active = fields.Boolean(
        string="Active Now",
        store=True,
        index=True,
        compute='_compute_is_active',
        readonly=True,
        help="Computed: TRUE iff is_published AND today is in valid window "
             "AND today's dow flag is set AND now is within start/end_time "
             "(0/0 = all day).",
    )

    DAY_NAME_MAP = {
        0: 'monday', 1: 'tuesday', 2: 'wednesday', 3: 'thursday',
        4: 'friday', 5: 'saturday', 6: 'sunday',
    }

    @api.depends(
        'is_published', 'valid_from', 'valid_until',
        'monday', 'tuesday', 'wednesday', 'thursday',
        'friday', 'saturday', 'sunday',
        'start_time', 'end_time',
    )
    def _compute_is_active(self):
        now = fields.Datetime.context_timestamp(self, fields.Datetime.now())
        today = now.date()
        hour_now = now.hour + now.minute / 60.0
        dow_name = self.DAY_NAME_MAP[today.weekday()]
        for rec in self:
            in_window = bool(rec.is_published) \
                and (not rec.valid_from or rec.valid_from <= today) \
                and (not rec.valid_until or rec.valid_until >= today)
            dow_ok = bool(getattr(rec, dow_name, False))
            st = rec.start_time or 0.0
            et = rec.end_time or 0.0
            time_ok = (st == 0 and et == 0) or (st <= hour_now <= et)
            rec.is_active = in_window and dow_ok and time_ok

    # ── Daily lifecycle cron (was: flip is_active, now: flip is_published) ──
    @api.model
    def _cron_ptl_daily_lifecycle(self):
        """Daily lifecycle for PTL discounts: publish on valid_from, unpublish past valid_until."""
        today = fields.Date.today()

        # Publish PTL deals where valid_from <= today
        to_publish = self.search([
            ('source', '=', 'ptl'),
            ('is_published', '=', False),
            ('valid_from', '<=', today),
            '|', ('valid_until', '=', False), ('valid_until', '>=', today),
        ])
        if to_publish:
            to_publish.write({'is_published': True})
            _logger.info('PTL lifecycle: published %d discounts', len(to_publish))

        # Unpublish PTL deals where valid_until < today
        to_unpublish = self.search([
            ('source', '=', 'ptl'),
            ('is_published', '=', True),
            ('valid_until', '!=', False),
            ('valid_until', '<', today),
        ])
        if to_unpublish:
            to_unpublish.write({'is_published': False})
            _logger.info('PTL lifecycle: unpublished %d expired discounts', len(to_unpublish))

        # Recompute day-of-week booleans for active PTL discounts
        active_ptl = self.search([
            ('source', '=', 'ptl'),
            ('is_published', '=', True),
            ('ptl_deal_id', '!=', False),
        ])
        for discount in active_ptl:
            self._recompute_day_booleans(discount)

        # Push all changes to inventory service
        changed = to_publish | to_unpublish | active_ptl
        if changed:
            self.env['mint.ptl.day']._push_discounts_to_redis(changed.ids)

        _logger.info(
            'PTL lifecycle cron: %d published, %d unpublished, %d dow-recomputed',
            len(to_publish), len(to_unpublish), len(active_ptl),
        )

    # ── Hourly cron: push only edges (is_active flips) to Redis ──────────
    @api.model
    def _cron_recompute_active(self):
        """Hourly: recompute is_active edges, push only diffs to Redis.

        is_active is a stored compute, so we invalidate + read to force re-store.
        Then we diff against the captured 'before' set and push only deltas to
        keep webhook traffic low.
        """
        candidates = self.search([('is_published', '=', True)])
        if not candidates:
            return
        before = {r.id: r.is_active for r in candidates}
        candidates.invalidate_recordset(['is_active'])
        # Force the read to materialize the new compute values:
        _ = [r.is_active for r in candidates]
        flipped = [r.id for r in candidates if before.get(r.id) != r.is_active]
        if flipped:
            self.env['mint.ptl.day']._push_discounts_to_redis(flipped)
            _logger.info(
                'PTL hourly active recompute: %d records flipped, pushed to Redis',
                len(flipped),
            )
        else:
            _logger.debug('PTL hourly active recompute: no edges')

    def _recompute_day_booleans(self, discount):
        """Recompute monday-sunday booleans from linked PTL day dates."""
        deal = discount.ptl_deal_id
        if not deal:
            return

        today = fields.Date.today()
        future_cutoff = today + timedelta(days=60)

        days = self.env['mint.ptl.day'].search([
            ('deal_ids', 'in', deal.id),
            ('state', '=', 'published'),
            ('date', '>=', today),
            ('date', '<=', future_cutoff),
        ])

        day_bools = {name: False for name in self.DAY_NAME_MAP.values()}
        for day_rec in days:
            weekday_num = day_rec.date.weekday()
            day_bools[self.DAY_NAME_MAP[weekday_num]] = True

        discount.write(day_bools)
