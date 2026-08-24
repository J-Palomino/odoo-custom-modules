# -*- coding: utf-8 -*-
"""Spin-to-win prize pool.

Every prize the wheel can award exists as a row here BEFORE anyone can win it.
Claiming draws one available row and marks it spent, so the pool itself is the
distribution cap — you cannot give away more 30%-off coupons than you created.
That is deliberate: Dutchie enforces MaxRedemptions but never populates
RedemptionCount, so a cap expressed only over there is invisible. Here, "how
many are left" is a COUNT.

The prize is decided at CLAIM time, not spin time. Nothing about the outcome
travels through the browser, so there is no token to sign and no secret to
manage — the customer's own account is the only thing tying them to a prize.

── Two different double-spends, two different guards ──────────────────────
1. Two concurrent claims must not draw the SAME row. Guarded by
   SELECT ... FOR UPDATE SKIP LOCKED, which hands each transaction a
   different row instead of blocking or colliding.
2. One customer must not claim TWICE in a day. Guarded by a unique
   constraint on (claimed_partner_id, claim_date) — not by a search-then-
   create check, which is a time-of-check/time-of-use race that concurrent
   requests slip straight through. Postgres treats NULLs as distinct, so
   unclaimed rows (both columns NULL) coexist freely while a second claim by
   the same partner on the same date raises.
"""
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class MintSpinPrize(models.Model):
    _name = 'mint.spin.prize'
    _description = 'Spin-to-Win Prize Pool Entry'
    _order = 'id'

    percent = fields.Integer(
        string='Percent Off', required=True,
        help='Discount awarded when this entry is claimed.',
    )
    state = fields.Selection(
        [('available', 'Available'), ('claimed', 'Claimed'), ('void', 'Void')],
        default='available', required=True, index=True,
    )
    batch = fields.Char(
        string='Batch', index=True,
        help='Free-form label for the run that created this entry, so a '
             'campaign can be counted, paused or voided as a unit.',
    )
    claimed_partner_id = fields.Many2one('res.partner', string='Claimed By', index=True)
    claim_date = fields.Date(
        string='Claim Date',
        help='Game date (America/Phoenix) the claim happened on. Part of the '
             'one-claim-per-customer-per-day constraint.',
    )
    claimed_at = fields.Datetime(string='Claimed At')
    discount_id = fields.Many2one(
        'mint.discount', string='Issued Coupon', ondelete='set null',
        help='The personal coupon minted for this entry.',
    )

    _sql_constraints = [
        # THE double-claim guard. See the module docstring: a search-then-create
        # check cannot hold under concurrency, this can.
        ('one_claim_per_partner_per_day',
         'UNIQUE(claimed_partner_id, claim_date)',
         'A customer can only claim one spin prize per day.'),
        ('percent_positive',
         'CHECK(percent > 0)',
         'A prize must award a real discount — there are no losing entries.'),
    ]

    @api.model
    def seed_pool(self, distribution, batch):
        """Create a batch of available prize entries.

        `distribution` maps percent -> how many entries to create, e.g.
        {5: 350, 10: 300, 15: 200, 20: 100, 25: 40, 30: 10}. The mix IS the
        odds: with that batch a 30% is 1-in-100, and exactly ten of them exist.
        Re-run with a new `batch` label to top the pool up.
        """
        if not batch:
            raise UserError(_("A batch label is required."))
        vals = []
        for percent, count in (distribution or {}).items():
            if int(percent) <= 0 or int(count) < 0:
                raise UserError(_("Invalid distribution entry: %s -> %s") % (percent, count))
            vals.extend(
                [{'percent': int(percent), 'batch': batch, 'state': 'available'}]
                * int(count)
            )
        if not vals:
            raise UserError(_("Distribution produced no entries."))
        return self.sudo().create(vals)

    @api.model
    def available_count(self, batch=None):
        domain = [('state', '=', 'available')]
        if batch:
            domain.append(('batch', '=', batch))
        return self.sudo().search_count(domain)

    @api.model
    def _draw_available_id(self):
        """Lock and return one available entry id, or None if the pool is dry.

        SKIP LOCKED is what makes this safe under load: a concurrent claim
        already holding row N is passed over rather than waited on, so two
        simultaneous claims get two different prizes instead of one blocking
        the other (or worse, both reading the same row before either writes).

        ORDER BY random() is fine at pool sizes measured in thousands. If the
        pool ever grows past that, switch to a TABLESAMPLE or a precomputed
        shuffle — but do NOT drop the FOR UPDATE SKIP LOCKED.
        """
        self.env.cr.execute("""
            SELECT id
              FROM mint_spin_prize
             WHERE state = 'available'
             ORDER BY random()
             LIMIT 1
               FOR UPDATE SKIP LOCKED
        """)
        row = self.env.cr.fetchone()
        return row[0] if row else None

    @api.model
    def claim_next(self, partner, claim_date, store=None):
        """Draw one prize for `partner` and mint their coupon.

        Returns (entry, created). `created` is False when the customer had
        already claimed that day — the caller turns that into a 409 and shows
        them the coupon they already have.

        Raises UserError when the pool is empty, which the caller surfaces as a
        503: an empty pool is an ops problem (nobody topped it up), not a
        customer error.
        """
        if not partner:
            raise UserError(_("Partner is required."))
        if not claim_date:
            raise UserError(_("A claim date is required."))

        # Already claimed today? Hand back the same entry rather than drawing a
        # second one. Checked first so the common repeat-visit case never
        # consumes pool inventory.
        existing = self.sudo().search([
            ('claimed_partner_id', '=', partner.id),
            ('claim_date', '=', claim_date),
        ], limit=1)
        if existing:
            return existing, False

        entry_id = self._draw_available_id()
        if not entry_id:
            _logger.error('spin pool exhausted — no available entries to claim')
            raise UserError(_("All prizes have been claimed. Please try again later."))

        entry = self.sudo().browse(entry_id)

        # Mint the coupon BEFORE marking the entry spent: if issuing fails, the
        # exception rolls the whole transaction back and the entry stays
        # available, rather than burning inventory on a customer who got
        # nothing.
        discount = self.env['mint.discount'].sudo().create_spin_prize_coupon(
            partner=partner,
            percent=entry.percent,
            claim_date=claim_date,
            pool_entry=entry,
            store=store,
        )

        # The unique constraint fires here if a concurrent request claimed for
        # this partner+date between the search above and now. That is the
        # intended outcome — the caller catches it and reports "already
        # claimed" instead of issuing a second coupon.
        entry.write({
            'state': 'claimed',
            'claimed_partner_id': partner.id,
            'claim_date': claim_date,
            'claimed_at': fields.Datetime.now(),
            'discount_id': discount.id,
        })
        return entry, True
