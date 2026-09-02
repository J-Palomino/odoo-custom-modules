# -*- coding: utf-8 -*-
"""The gift card ledger — money invariants.

Every assertion here is one of the ways a stored-value instrument leaks. They
are not hypothetical: the behaviour this module exists to replace was measured
live on `MINT-GMXT6F`, a coupon issued as "$100 off, 10 uses" that paid out
$300 across four partial draws ($95 / $60 / $85 / $60) because a Dutchie
redemption cap counts USES, not dollars.

The property that makes this a gift card rather than a coupon is exercised in
test_remainder_survives_partial_redemption: spend part, and the rest is still
there.
"""
from datetime import timedelta

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged("post_install", "-at_install")
class TestGiftCardLedger(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Card = cls.env["mint.gift.card"]
        cls.Line = cls.env["mint.gift.card.line"]

    def _card(self, face=100.0, activate=True, **kw):
        card = self.Card.create(dict({
            "face_value": face,
            "issue_reason": "service_recovery",
        }, **kw))
        if activate:
            card.action_activate()
        return card

    # ── The core property ───────────────────────────────────────────────
    def test_remainder_survives_partial_redemption(self):
        """Spend $30 of $100 and $70 must still be spendable.

        This is the whole reason the module exists. A Dutchie coupon in the
        same position destroys the unspent $70.
        """
        card = self._card(100.0)
        line = card.hold(30.0)
        line.action_settle(30.0, order_id="178720095")

        self.assertEqual(card.settled_amount, 30.0)
        self.assertEqual(card.held_amount, 0.0)
        self.assertEqual(card.balance, 70.0, "the remainder must survive")
        self.assertTrue(card.is_spendable)

        # And the remainder is genuinely usable, to the cent.
        second = card.hold(70.0)
        second.action_settle(70.0)
        self.assertEqual(card.balance, 0.0)
        self.assertEqual(card.state, "depleted")

    def test_draw_cannot_exceed_balance(self):
        """The failure mode that turned a $100 coupon into $300."""
        card = self._card(100.0)
        card.hold(60.0).action_settle(60.0)
        with self.assertRaises(UserError):
            card.hold(41.0)
        # The refused draw left nothing behind.
        self.assertEqual(card.balance, 40.0)
        self.assertEqual(len(card.line_ids), 1)

    def test_max_drawable_reflects_spendability(self):
        card = self._card(50.0)
        self.assertEqual(card.max_drawable(), 50.0)
        card.hold(20.0)
        self.assertEqual(card.max_drawable(), 30.0, "an open hold is not spendable twice")
        card.action_void()
        self.assertEqual(card.max_drawable(), 0.0)

    # ── Hold / settle / release ─────────────────────────────────────────
    def test_hold_is_provisional_not_final(self):
        card = self._card(100.0)
        card.hold(25.0)
        self.assertEqual(card.held_amount, 25.0)
        self.assertEqual(card.settled_amount, 0.0)
        self.assertEqual(card.balance, 75.0, "a hold reduces the spendable balance")

    def test_release_returns_the_money(self):
        """An abandoned basket must not quietly cost the customer."""
        card = self._card(100.0)
        line = card.hold(40.0)
        line.action_release(reason="basket abandoned")
        self.assertEqual(card.balance, 100.0)
        self.assertEqual(card.held_amount, 0.0)
        self.assertEqual(line.state, "released")

    def test_settling_less_than_drawn_returns_the_difference(self):
        """The basket can shrink between apply and checkout."""
        card = self._card(100.0)
        line = card.hold(50.0)
        line.action_settle(35.0)
        self.assertEqual(card.settled_amount, 35.0)
        self.assertEqual(card.balance, 65.0, "the unspent $15 goes back")

    def test_cannot_settle_more_than_drawn(self):
        """Dutchie reporting more than we authorised must not drain the card."""
        card = self._card(100.0)
        line = card.hold(20.0)
        with self.assertRaises(UserError):
            line.action_settle(35.0)
        self.assertEqual(card.balance, 80.0)

    def test_settle_is_terminal(self):
        card = self._card(100.0)
        line = card.hold(10.0)
        line.action_settle(10.0)
        with self.assertRaises(UserError):
            line.action_settle(10.0)

    def test_failed_draw_frees_the_balance_but_stays_visible(self):
        card = self._card(100.0)
        line = card.hold(30.0)
        line.action_fail(reason="apply-by-code rejected")
        self.assertEqual(card.balance, 100.0)
        self.assertEqual(line.state, "failed", "distinguishable from a normal release")

    # ── Concurrency and retries ─────────────────────────────────────────
    def test_idempotency_key_prevents_a_double_draw(self):
        """A retry or an impatient double-tap must not draw twice."""
        card = self._card(100.0)
        first = card.hold(25.0, idempotency_key="shipment-42")
        second = card.hold(25.0, idempotency_key="shipment-42")
        self.assertEqual(first, second, "the original hold is returned, not a new one")
        self.assertEqual(len(card.line_ids), 1)
        self.assertEqual(card.balance, 75.0)

    # ── State machine ───────────────────────────────────────────────────
    def test_draft_card_is_not_spendable(self):
        card = self._card(100.0, activate=False)
        self.assertEqual(card.state, "draft")
        self.assertFalse(card.is_spendable)
        with self.assertRaises(UserError):
            card.hold(10.0)

    def test_expired_card_is_not_spendable(self):
        card = self._card(100.0, expires_at=fields.Date.today() - timedelta(days=1))
        self.assertFalse(card.is_spendable, "past its expiry, even while 'active'")
        with self.assertRaises(UserError):
            card.hold(10.0)

    def test_void_releases_outstanding_holds(self):
        card = self._card(100.0)
        line = card.hold(30.0)
        card.action_void()
        self.assertEqual(card.state, "void")
        self.assertEqual(line.state, "released", "a void must not strand a hold")
        self.assertFalse(card.is_spendable)

    def test_depleted_flips_back_when_a_hold_is_released(self):
        card = self._card(50.0)
        line = card.hold(50.0)
        self.assertEqual(card.state, "depleted")
        line.action_release(reason="basket abandoned")
        self.assertEqual(card.state, "active", "money came back, so the card is live again")

    # ── Immutability of the audit trail ─────────────────────────────────
    def test_draws_cannot_be_deleted(self):
        card = self._card(100.0)
        line = card.hold(10.0)
        with self.assertRaises(UserError):
            line.unlink()

    def test_face_value_is_frozen_once_live(self):
        """Editing it would retroactively rewrite every balance ever shown."""
        card = self._card(100.0)
        with self.assertRaises(UserError):
            card.face_value = 250.0

    def test_face_value_is_editable_in_draft(self):
        card = self._card(100.0, activate=False)
        card.face_value = 250.0
        self.assertEqual(card.face_value, 250.0)

    # ── Codes ───────────────────────────────────────────────────────────
    def test_codes_are_unique_and_prefixed(self):
        codes = {self._card(10.0, activate=False).code for _ in range(25)}
        self.assertEqual(len(codes), 25, "generated codes must not collide")
        for code in codes:
            self.assertTrue(code.startswith("MINT-GC-"))
            # No ambiguous glyphs — staff read these off a screen.
            self.assertNotRegex(code[len("MINT-GC-"):], r"[01ILOU]")

    # ── Cron ────────────────────────────────────────────────────────────
    def test_cron_releases_stale_holds_and_expires_cards(self):
        card = self._card(100.0)
        stale = card.hold(30.0)
        # Backdate past the release window without touching the cron's clock.
        stale.held_at = fields.Datetime.now() - timedelta(hours=48)

        dated = self._card(20.0)
        dated.expires_at = fields.Date.today() - timedelta(days=2)

        self.env["mint.gift.card"]._cron_expire_and_release()

        self.assertEqual(stale.state, "released")
        self.assertEqual(card.balance, 100.0, "an unconfirmed hold returns to the card")
        self.assertEqual(dated.state, "expired")

    def test_cron_leaves_fresh_holds_alone(self):
        """A live redemption must not be released out from under itself."""
        card = self._card(100.0)
        fresh = card.hold(30.0)
        self.env["mint.gift.card"]._cron_expire_and_release()
        self.assertEqual(fresh.state, "held")
        self.assertEqual(card.balance, 70.0)
