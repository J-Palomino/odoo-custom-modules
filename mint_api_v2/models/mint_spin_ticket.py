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
import json
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
    source_ref = fields.Char(
        string='Source Reference', index=True,
        help='Idempotency key for automatic grants, e.g. '
             '"purchase:<company_id>:<receipt_no>". Deliberately a string '
             'rather than a foreign key: the granting module (mint_dutchie_sync) '
             'does not depend on this one, and a char key works for any future '
             'source without dragging in a dependency each time.',
    )
    source_seq = fields.Integer(
        string='Source Sequence', default=1,
        help='Which ticket of N for the same source_ref.',
    )

    _sql_constraints = [
        # Idempotency for automatic grants. A re-imported receipt tries the
        # same (ref, seq) and is rejected, so a catch-up sync cannot pay a
        # customer twice for one purchase. NULLs are distinct in Postgres, so
        # manual grants (no ref) are unaffected.
        ('one_ticket_per_source_seq',
         'UNIQUE(source_ref, source_seq)',
         'That ticket has already been granted for this source.'),
    ]

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
    def purchase_grant_settings(self):
        """Read the purchase->ticket rules from mint.config.

        Key: `spin.purchase_grant`, JSON, e.g.
            {"enabled": true, "tickets": 1, "min_spend": 25, "max_age_days": 3}

        DISABLED by default, on purpose. Two reasons this must be opt-in:
        granting tickets for a purchase is the change that makes this
        promotion purchase -> chance -> prize, and the purchase importer runs
        in bulk — switching it on with a wide `max_age_days` while the sync is
        behind would mint a backlog of tickets in one go.
        """
        defaults = {'enabled': False, 'tickets': 1, 'min_spend': 0.0,
                    'max_age_days': 3}
        row = self.env['mint.config'].sudo().search(
            [('key', '=', 'spin.purchase_grant'), ('is_active', '=', True)], limit=1)
        if not row or not row.value:
            return defaults
        try:
            settings = json.loads(row.value)
        except (ValueError, TypeError):
            _logger.warning('spin.purchase_grant is not valid JSON; grants stay off')
            return defaults
        if not isinstance(settings, dict):
            return defaults
        merged = dict(defaults)
        merged.update(settings)
        return merged

    @api.model
    def grant_for_ref(self, partner, source_ref, count=1, source='grant', **kw):
        """Grant tickets idempotently against an external reference.

        Safe to call repeatedly for the same reference: already-granted
        sequences are skipped, so a re-imported purchase adds nothing. Returns
        the tickets created by THIS call (empty if it was a replay).
        """
        if not partner or not source_ref:
            raise UserError(_("Partner and source_ref are required."))

        existing = self.sudo().search_count([('source_ref', '=', source_ref)])
        wanted = int(count) - existing
        if wanted < 1:
            return self.sudo().browse()

        vals = [{
            'partner_id': partner.id,
            'source': source,
            'source_ref': source_ref,
            'source_seq': existing + i + 1,
            'expires_at': kw.get('expires_at'),
            'batch': kw.get('batch'),
            'note': kw.get('note'),
        } for i in range(wanted)]
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
