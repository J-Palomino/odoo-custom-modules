# -*- coding: utf-8 -*-
"""
Loyalty point auditing and the automatic-award gate.

Two related problems this file solves.

**1. Point movements were invisible.**
A production audit on 2026-08-10 found 172,246 cards holding 9,716,529 points
between them, and exactly *three* ``loyalty.history`` rows in the whole
database — two of which were test artifacts. Every custom code path mutated
``loyalty.card.points`` with a bare ``write()``, so nothing recorded who moved
points, when, or why. When a card turned up with 417,999 points and one $10
purchase behind it, the balance could not be explained even in principle.

The fix is deliberately placed at the *model* layer rather than at each call
site: overriding ``write()``/``create()`` means every present and future
mutation is audited, including ad-hoc RPC writes by the 16 users who hold
``perm_write`` on ``loyalty.card``. Patching call sites would only cover the
ones we know about today.

**2. Odoo minted points Dutchie never granted.**
The importer below awarded a flat 1 point per dollar on every imported
purchase. Dutchie's Arizona program accrues ~0.8 pt/$1 *and* runs
``LoyaltyRequireOptIn = true``, so customers who never enrolled earn nothing
there. Sampling linked customers found 0 of 45 balances matching, with Odoo
inventing points for 25–64% of them (band-dependent) and under-reporting the
rest by 1.4–4.6x.

Until the reconciliation backfill runs, automatic awarding is gated off. The
gate is a config parameter rather than deleted code so it is reversible
without a deploy.
"""
import logging

from odoo import api, models

_logger = logging.getLogger(__name__)

#: ``ir.config_parameter`` controlling automatic point awards.
#:
#: - ``off``    (default) no automatic awarding. Redemptions still *deduct*,
#:              and voids still restore — only minting is suppressed.
#: - ``legacy`` restore the pre-2026-08 behaviour (flat 1 pt/$1 on import).
#:
#: Deliberately defaults to ``off`` when unset: an instance that has never
#: heard of this parameter is one that has not yet been reconciled against
#: Dutchie, and minting into an unreconciled ledger is what caused the drift.
AWARD_MODE_PARAM = 'mint.loyalty.award_mode'

#: Context key that suppresses audit-row creation for one operation. Used by
#: the balance wizard, which writes its own richer ``loyalty.history`` row and
#: would otherwise be double-logged.
SKIP_AUDIT_CTX = 'mint_skip_point_audit'

#: Context key carrying a human label for whatever is moving points. Anything
#: that mutates points should set it; unset simply reads as unattributed
#: rather than failing.
SOURCE_CTX = 'mint_point_source'


class LoyaltyCard(models.Model):
    _inherit = 'loyalty.card'

    # ------------------------------------------------------------------
    # Award gate
    # ------------------------------------------------------------------
    @api.model
    def _mint_award_mode(self):
        """Return the configured automatic-award mode. Never raises."""
        return (self.env['ir.config_parameter'].sudo()
                .get_param(AWARD_MODE_PARAM, 'off') or 'off').strip().lower()

    @api.model
    def _mint_awards_enabled(self):
        """True when automatic point awarding is permitted."""
        return self._mint_award_mode() == 'legacy'

    # ------------------------------------------------------------------
    # Audit trail
    # ------------------------------------------------------------------
    def write(self, vals):
        if 'points' not in vals or self.env.context.get(SKIP_AUDIT_CTX):
            return super().write(vals)
        # Snapshot before the write so the delta is exact even when `vals`
        # is a computed absolute value rather than a delta.
        before = {card.id: card.points for card in self}
        result = super().write(vals)
        self._mint_log_point_changes(before)
        return result

    @api.model_create_multi
    def create(self, vals_list):
        cards = super().create(vals_list)
        if not self.env.context.get(SKIP_AUDIT_CTX):
            # A card created with a non-zero opening balance is a point
            # movement too — it was the second half of the 417,999 mystery.
            cards._mint_log_point_changes({card.id: 0.0 for card in cards},
                                          opening=True)
        return cards

    def _mint_log_point_changes(self, before, opening=False):
        """Write one ``loyalty.history`` row per card whose balance moved.

        :param before: ``{card_id: points_before}``
        :param opening: label the row as an opening balance rather than a
            change to an existing one.
        """
        rows = []
        source = self.env.context.get(SOURCE_CTX) or 'direct write (unattributed)'
        for card in self:
            delta = card.points - before.get(card.id, 0.0)
            if not delta:
                continue
            rows.append({
                'card_id': card.id,
                'issued': delta if delta > 0 else 0.0,
                'used': -delta if delta < 0 else 0.0,
                'description': '%s: %+.2f -> %.2f (%s)' % (
                    'Opening balance' if opening else 'Balance change',
                    delta, card.points, source,
                ),
            })
        if not rows:
            return
        # sudo(): the audit trail must not be defeatable by the writer's own
        # access rights. If a user can move points, the movement gets recorded.
        self.env['loyalty.history'].sudo().create(rows)
        _logger.info('Loyalty audit: recorded %d point movement(s) [%s]',
                     len(rows), source)

    # ------------------------------------------------------------------
    # Customer notification (manual grants only)
    # ------------------------------------------------------------------
    def _mint_notify_points_issued(self, delta):
        """Tell the customer their balance just went up.

        Called ONLY from the manual-adjustment wizard below, deliberately not
        from the write()/create() audit layer: the audit layer also sees bulk
        reconciliations and importer runs, and a backfill touching 80k cards
        must never fan out 80k text messages.

        Consent is enforced by the channels themselves, not here:
        - SMS: ``mint.sms.message.create()`` IS the send, and its
          deterministic gate (SMS Whitelist tag + sms_opt_in + not
          sms_opt_out + content guardrail) blocks unauthorized recipients
          with state='blocked' — creating the row for a non-consenting
          partner records the attempt without texting anyone.
        - Push: a ``mint.push.subscription`` row only exists for a browser
          whose user granted notification permission; no subscription, no
          send.

        Both channels are best-effort inside savepoints: a notification
        failure must never roll back the grant itself. Neither module is a
        dependency of this one (both are installed on prod), so each is
        probed in the registry first.
        """
        self.ensure_one()
        partner = self.partner_id
        if not partner or delta <= 0:
            return
        balance = self.points or 0.0
        body = ('You just received %g Mint Rewards points! Your balance is '
                'now %g. View your rewards: '
                'https://shop.letsgomint.us/rewards' % (delta, balance))

        if 'mint.sms.message' in self.env:
            try:
                with self.env.cr.savepoint():
                    # sudo: the operator granting points need not be an "SMS
                    # Sender"; the consent gate inside create() is
                    # user-independent. partner_id passed explicitly — number
                    # -> partner resolution can pick a duplicate partner and
                    # miss the whitelist (seen on prod 2026-08-11).
                    self.env['mint.sms.message'].sudo().create({
                        'partner_id': partner.id,
                        'body': body,
                    })
            except Exception:
                _logger.exception(
                    'Points-issued SMS failed for partner %s (card %s)',
                    partner.id, self.id)

        if 'mint.push.subscription' in self.env:
            try:
                with self.env.cr.savepoint():
                    sent = self.env['mint.push.subscription'].sudo().send_to_partner(
                        partner.id,
                        'Mint Rewards',
                        '%g points added — your balance is now %g.' % (delta, balance),
                        url='/rewards',
                    )
                    _logger.info(
                        'Points-issued push: %s subscription(s) reached for '
                        'partner %s (card %s)', sent, partner.id, self.id)
            except Exception:
                _logger.exception(
                    'Points-issued push failed for partner %s (card %s)',
                    partner.id, self.id)


class LoyaltyCardUpdateBalance(models.TransientModel):
    """The manual-adjustment wizard already writes its own history row.

    Without this, an admin adjustment would produce two rows: the wizard's
    (carrying the operator's typed reason) and ours (carrying only a delta).
    The wizard's is strictly more informative, so ours stands down.
    """
    _inherit = 'loyalty.card.update.balance'

    def action_update_card_point(self):
        # Snapshot balances before the write so the issued delta is exact —
        # new_balance is an absolute, not a delta.
        before = {w.id: w.card_id.points or 0.0 for w in self}
        res = super(
            LoyaltyCardUpdateBalance,
            self.with_context(**{SKIP_AUDIT_CTX: True}),
        ).action_update_card_point()
        # Notify AFTER the grant is committed to the record: an increase in
        # balance is customer-visible good news; decreases (corrections,
        # zeroings) notify nobody.
        for wizard in self:
            delta = (wizard.card_id.points or 0.0) - before[wizard.id]
            if delta > 0:
                wizard.card_id._mint_notify_points_issued(delta)
        return res
