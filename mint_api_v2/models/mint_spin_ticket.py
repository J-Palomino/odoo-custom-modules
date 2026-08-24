# -*- coding: utf-8 -*-
"""Spin-to-win tickets.

A ticket is the right to one spin. No ticket, no spin — the wheel is not a
free-for-all, it is spend-one-get-one.

── The `source` field is a legal control, not a label ─────────────────────
Where a ticket came from decides what this promotion legally IS. A ticket
granted at signup, by staff, or through a promo keeps the wheel a discount
giveaway. A ticket earned by PURCHASE turns it into purchase -> chance ->
prize, which is the classic lottery triad, and needs a free alternative method
of entry (and a lawyer) before it goes anywhere near production. The field is
explicit so that decision is visible in the data rather than buried in
whoever wrote the grant script.

── Double-spend ───────────────────────────────────────────────────────────
Spending is a single atomic UPDATE ... WHERE state='available' ... RETURNING.
Postgres either hands this transaction the row or it does not; there is no
window between "check the balance" and "decrement it" for a second request to
slip through. A read-then-write would have exactly that window, and a customer
double-clicking Reveal is enough to find it.
"""
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class MintSpinTicket(models.Model):
    _name = 'mint.spin.ticket'
    _description = 'Spin-to-Win Ticket'
    _order = 'id'

    partner_id = fields.Many2one(
        'res.partner', string='Customer', required=True, index=True, ondelete='cascade',
    )
    state = fields.Selection(
        [('available', 'Available'), ('spent', 'Spent'),
         ('void', 'Void'), ('expired', 'Expired')],
        default='available', required=True, index=True,
    )
    source = fields.Selection(
        [('grant', 'Staff Grant'),
         ('signup', 'Signup Bonus'),
         ('promo', 'Promotion'),
         ('points', 'Loyalty Points'),
         ('purchase', 'Purchase')],
        default='grant', required=True, index=True,
        help='Where this ticket came from. See the module docstring: a '
             'purchase-sourced ticket changes what this promotion legally is.',
    )
    batch = fields.Char(string='Batch', index=True)
    note = fields.Char(string='Note')
    granted_at = fields.Datetime(default=lambda self: fields.Datetime.now())
    expires_at = fields.Datetime(
        string='Expires At',
        help='Optional. Expired tickets are skipped when spending and can be '
             'swept to state=expired.',
    )
    spent_at = fields.Datetime(string='Spent At')
    prize_id = fields.Many2one(
        'mint.spin.prize', string='Prize Won', ondelete='set null',
        help='The pool entry this ticket bought.',
    )

    @api.model
    def grant(self, partner, count=1, source='grant', expires_at=None,
              batch=None, note=None):
        """Give a customer `count` spins."""
        if not partner:
            raise UserError(_("Partner is required."))
        if int(count) < 1:
            raise UserError(_("Grant at least one ticket."))
        vals = [{
            'partner_id': partner.id,
            'source': source,
            'expires_at': expires_at,
            'batch': batch,
            'note': note,
        }] * int(count)
        return self.sudo().create(vals)

    @api.model
    def available_count(self, partner):
        """How many spins this customer has right now."""
        if not partner:
            return 0
        return self.sudo().search_count([
            ('partner_id', '=', partner.id),
            ('state', '=', 'available'),
            '|', ('expires_at', '=', False), ('expires_at', '>', fields.Datetime.now()),
        ])

    @api.model
    def spend_one(self, partner):
        """Atomically spend one ticket. Returns the record, or None if broke.

        Soonest-expiring first (NULLS LAST) so a customer never loses a dated
        ticket while an open-ended one sits in front of it.

        This is deliberately raw SQL: the whole point is that the select and
        the state change are ONE statement, so two concurrent reveals cannot
        both be handed the same ticket. FOR UPDATE SKIP LOCKED makes the loser
        of that race see the next ticket (or none) instead of blocking.
        """
        if not partner:
            return None
        self.env.cr.execute("""
            UPDATE mint_spin_ticket
               SET state = 'spent', spent_at = now()
             WHERE id = (
                 SELECT id
                   FROM mint_spin_ticket
                  WHERE partner_id = %s
                    AND state = 'available'
                    AND (expires_at IS NULL OR expires_at > now())
                  ORDER BY expires_at ASC NULLS LAST, id ASC
                  LIMIT 1
                    FOR UPDATE SKIP LOCKED
             )
         RETURNING id
        """, (partner.id,))
        row = self.env.cr.fetchone()
        if not row:
            return None
        # Invalidate so the ORM does not serve a cached state='available'.
        self.env['mint.spin.ticket'].invalidate_model(['state', 'spent_at'])
        return self.sudo().browse(row[0])

    @api.model
    def sweep_expired(self):
        """Cron-friendly: flip elapsed tickets to `expired`."""
        stale = self.sudo().search([
            ('state', '=', 'available'),
            ('expires_at', '!=', False),
            ('expires_at', '<=', fields.Datetime.now()),
        ])
        if stale:
            stale.write({'state': 'expired'})
        return len(stale)
