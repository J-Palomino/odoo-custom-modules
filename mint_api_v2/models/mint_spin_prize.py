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

── Three double-spends, three guards ──────────────────────────────────────
1. Two concurrent claims must not draw the SAME pool row. Guarded by
   SELECT ... FOR UPDATE SKIP LOCKED, which hands each transaction a
   different row instead of blocking or colliding.
2. One ticket must not buy TWO prizes. Guarded by UNIQUE(ticket_id) — a DB
   constraint, not a check, so a retry that slips past the application logic
   still cannot produce a second prize from the same ticket.
3. One ticket must not be spent twice. Guarded in mint.spin.ticket by an
   atomic UPDATE ... RETURNING (see that model).

Note what is NOT a guard any more: a per-day cap. Spins are limited by how
many tickets a customer holds, so someone with three tickets may spin three
times today. The ticket is the gate.
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
    ticket_id = fields.Many2one(
        'mint.spin.ticket', string='Ticket Spent', ondelete='restrict', index=True,
        help='The ticket the customer spent to win this. One ticket, one prize.',
    )

    # NOTE: declared with models.Constraint, NOT the legacy _sql_constraints
    # list. Odoo 19 accepts that list without error and never creates the
    # constraint — no warning, no index, and every guarantee below silently
    # becomes a no-op.

    # THE one-prize-per-ticket guard. A real DB constraint rather than a check,
    # so a retry that slips past the application logic still cannot mint a
    # second prize from a ticket that was already cashed. NULLs are distinct in
    # Postgres, so unclaimed pool rows coexist freely.
    _one_prize_per_ticket = models.Constraint(
        'UNIQUE(ticket_id)',
        'That ticket has already been redeemed for a prize.',
    )
    _percent_positive = models.Constraint(
        'CHECK(percent > 0)',
        'A prize must award a real discount — there are no losing entries.',
    )

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
        """Spend one of the customer's tickets and draw them a prize.

        Returns (entry, created). Raises UserError when the customer has no
        ticket, or when the pool is empty — the caller maps those to 403 and
        503 respectively, because they are different problems: one is "you
        need a ticket", the other is "ops did not top up the pool".

        Order matters. The ticket is spent FIRST, so two concurrent reveals
        cannot both proceed on one ticket. Everything after runs in the same
        transaction, so any failure — an empty pool, a coupon that will not
        issue — rolls the spend back and the customer keeps their ticket.
        """
        if not partner:
            raise UserError(_("Partner is required."))
        if not claim_date:
            raise UserError(_("A claim date is required."))

        Ticket = self.env['mint.spin.ticket'].sudo()
        ticket = Ticket.spend_one(partner)
        if not ticket:
            raise UserError(_("You need a spin ticket to play. "
                              "Earn one in your rewards."))

        entry_id = self._draw_available_id()
        if not entry_id:
            # Rolls back the spend with it: the customer keeps their ticket.
            _logger.error('spin pool exhausted — no available entries to claim')
            raise UserError(_("All prizes have been claimed. Please try again later."))

        entry = self.sudo().browse(entry_id)

        # Mint the coupon BEFORE marking the entry spent: if issuing fails, the
        # exception rolls the whole transaction back and both the entry and the
        # ticket stay available, rather than burning inventory on a customer
        # who got nothing.
        discount = self.env['mint.discount'].sudo().create_spin_prize_coupon(
            partner=partner,
            percent=entry.percent,
            claim_date=claim_date,
            pool_entry=entry,
            store=store,
        )

        # UNIQUE(ticket_id) fires here if this ticket somehow already bought a
        # prize. That is the intended outcome — better a hard failure than a
        # second coupon.
        entry.write({
            'state': 'claimed',
            'claimed_partner_id': partner.id,
            'claim_date': claim_date,
            'claimed_at': fields.Datetime.now(),
            'discount_id': discount.id,
            'ticket_id': ticket.id,
        })
        ticket.write({'prize_id': entry.id})
        return entry, True
