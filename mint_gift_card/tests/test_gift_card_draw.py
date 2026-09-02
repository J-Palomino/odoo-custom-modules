# -*- coding: utf-8 -*-
"""The draw engine's arithmetic and its refusals.

Every test here is about picking the right NUMBER, because that is the part
that silently costs a customer money. A draw that is too large destroys the
difference (a coupon gives no change); one that is too small leaves them
paying cash they should not have to.

invsvc is stubbed throughout — the basket read is a network call and these
assertions are about what we do with the answer, not whether the network
works.
"""
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


def cart(grand=0.0, sub=None, items=1):
    return {
        "items": [{"productId": 1, "name": "x", "category": "Flower"}] * items,
        "total_items": items,
        "grand_total": grand,
        "sub_total": sub if sub is not None else grand,
        "applied_discount_ids": [],
    }


@tagged("post_install", "-at_install")
class TestGiftCardDraw(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Card = cls.env["mint.gift.card"]

    def _card(self, face=100.0):
        card = self.Card.create({"face_value": face, "issue_reason": "promotion"})
        card.action_activate()
        return card

    # ── The amount ──────────────────────────────────────────────────────
    def test_draw_is_capped_by_the_basket(self):
        """A $100 card against a $30 basket draws $30, not $100.

        Drawing the full face value would destroy $70, which is precisely the
        Dutchie behaviour this module exists to avoid.
        """
        card = self._card(100.0)
        self.assertEqual(card.compute_draw(cart(grand=30.0)), 30.0)

    def test_draw_is_capped_by_the_balance(self):
        card = self._card(25.0)
        self.assertEqual(card.compute_draw(cart(grand=80.0)), 25.0)

    def test_draw_on_an_exactly_matching_basket(self):
        card = self._card(40.0)
        self.assertEqual(card.compute_draw(cart(grand=40.0)), 40.0)

    def test_draw_accounts_for_an_open_hold(self):
        """Money already held for another basket is not available again."""
        card = self._card(100.0)
        card.hold(60.0)
        self.assertEqual(card.compute_draw(cart(grand=100.0)), 40.0)

    def test_depleted_card_draws_nothing(self):
        card = self._card(20.0)
        card.hold(20.0).action_settle(20.0)
        self.assertEqual(card.compute_draw(cart(grand=50.0)), 0.0)

    # ── Which total we draw against ─────────────────────────────────────
    def test_basis_defaults_to_grand_total(self):
        """A customer handing over a card expects it against the pin-pad
        number, tax included."""
        card = self._card(100.0)
        self.assertEqual(card._draw_basis(), "grand_total")
        self.assertEqual(card.basket_payable(cart(grand=54.0, sub=48.0)), 54.0)

    def test_basis_is_switchable_to_sub_total(self):
        """The live basket test may say otherwise; this must not need a deploy."""
        self.env["ir.config_parameter"].sudo().set_param(
            "mint_gift_card.draw_basis", "sub_total")
        card = self._card(100.0)
        self.assertEqual(card.basket_payable(cart(grand=54.0, sub=48.0)), 48.0)

    # ── plan_draw refusals ──────────────────────────────────────────────
    def test_plan_refuses_an_unreadable_basket(self):
        """Unreadable is not empty. Guessing the amount is worse than refusing."""
        card = self._card(100.0)
        with patch.object(type(card), "read_basket", return_value=None):
            res = card.plan_draw(2679, 575, "SHIP-1")
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "cart_unreadable")

    def test_plan_refuses_an_empty_basket(self):
        card = self._card(100.0)
        with patch.object(type(card), "read_basket", return_value=cart(grand=0.0, items=0)):
            res = card.plan_draw(2679, 575, "SHIP-1")
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "nothing_to_pay")

    def test_plan_refuses_a_dead_card(self):
        card = self._card(100.0)
        card.action_void()
        with patch.object(type(card), "read_basket", return_value=cart(grand=50.0)):
            res = card.plan_draw(2679, 575, "SHIP-1")
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "card_not_spendable")

    def test_plan_refuses_a_store_outside_the_cards_scope(self):
        """store_ids gates OUR draw path — the only place scope is enforced,
        since a minted child is LSP-wide at the register regardless."""
        company = self.env["res.company"].search([], limit=1)
        card = self._card(100.0)
        card.store_ids = [(6, 0, company.ids)]
        with patch.object(type(card), "read_basket", return_value=cart(grand=50.0)), \
             patch.object(type(self.env["mint.ptl.day"]), "_resolve_pos_loc_id",
                          return_value=9999):
            res = card.plan_draw(2679, 575, "SHIP-1")
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "store_not_allowed")

    # ── plan_draw success ───────────────────────────────────────────────
    def test_plan_reports_the_amount_and_the_remainder(self):
        card = self._card(100.0)
        with patch.object(type(card), "read_basket", return_value=cart(grand=30.0)):
            res = card.plan_draw(2679, 575, "SHIP-1")
        self.assertTrue(res["ok"])
        self.assertEqual(res["amount"], 30.0)
        self.assertEqual(res["balance_after"], 70.0)
        self.assertTrue(res["covers_basket"])

    def test_plan_flags_a_basket_the_card_cannot_cover(self):
        """The customer will owe the difference — the UI needs to say so."""
        card = self._card(20.0)
        with patch.object(type(card), "read_basket", return_value=cart(grand=75.0)):
            res = card.plan_draw(2679, 575, "SHIP-1")
        self.assertTrue(res["ok"])
        self.assertEqual(res["amount"], 20.0)
        self.assertFalse(res["covers_basket"])
        self.assertEqual(res["balance_after"], 0.0)

    def test_plan_writes_nothing(self):
        """A dry run against production must leave no trace."""
        card = self._card(100.0)
        before = len(card.line_ids)
        with patch.object(type(card), "read_basket", return_value=cart(grand=30.0)):
            card.plan_draw(2679, 575, "SHIP-1")
        self.assertEqual(len(card.line_ids), before)
        self.assertEqual(card.balance, 100.0)


@tagged("post_install", "-at_install")
class TestGiftCardExecuteDraw(TransactionCase):
    """The write path, and above all its failure paths.

    A draw that half-succeeds is the dangerous case: a coupon minted and then
    abandoned is spendable money sitting in Dutchie with nothing on the ledger
    holding it. Every failure below must end with the balance restored AND the
    child coupon retired.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Card = cls.env["mint.gift.card"]

    def _card(self, face=100.0):
        card = self.Card.create({"face_value": face, "issue_reason": "promotion"})
        card.action_activate()
        return card

    def _fake_child(self, code="MINT-GD-ABC123"):
        return self.env["mint.discount"].sudo().create({
            "name": "test child", "discount_type": "dollar_off_total",
            "application_method": "code", "code": code,
            "dutchie_discount_code": code, "maximum_usage_count": 1,
        })

    def test_successful_draw_holds_and_records_the_child(self):
        card = self._card(100.0)
        child = self._fake_child()
        T = type(card)
        with patch.object(T, "read_basket", return_value=cart(grand=30.0)), \
             patch.object(T, "_mint_child", return_value=(child, 999001)), \
             patch.object(T, "_apply_child", return_value=(True, "applied")):
            res = card.execute_draw(2679, 575, "SHIP-1")

        self.assertTrue(res["ok"])
        self.assertEqual(res["amount"], 30.0)
        self.assertEqual(card.balance, 70.0, "the remainder survives")
        line = card.line_ids
        self.assertEqual(len(line), 1)
        self.assertEqual(line.state, "held", "held until report 10875 confirms it")
        self.assertEqual(line.child_code, "MINT-GD-ABC123")
        self.assertEqual(line.dutchie_discount_id, "999001")

    def test_mint_failure_frees_the_money(self):
        card = self._card(100.0)
        T = type(card)
        with patch.object(T, "read_basket", return_value=cart(grand=30.0)), \
             patch.object(T, "_mint_child", side_effect=UserError("Dutchie said no")):
            res = card.execute_draw(2679, 575, "SHIP-1")

        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "mint_failed")
        self.assertEqual(card.balance, 100.0, "nothing was charged")
        self.assertEqual(card.line_ids.state, "failed")

    def test_refused_apply_retires_the_child_and_frees_the_money(self):
        """The most dangerous path: a coupon exists but never went on a basket."""
        card = self._card(100.0)
        child = self._fake_child("MINT-GD-DEAD01")
        T = type(card)
        with patch.object(T, "read_basket", return_value=cart(grand=30.0)), \
             patch.object(T, "_mint_child", return_value=(child, 999002)), \
             patch.object(T, "_apply_child", return_value=(False, "Invalid discount code.")), \
             patch.object(T, "_retire_child", return_value=True) as retire:
            res = card.execute_draw(2679, 575, "SHIP-1")

        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "apply_refused")
        retire.assert_called_once()
        self.assertEqual(card.balance, 100.0, "a refused coupon costs nothing")
        self.assertEqual(card.line_ids.state, "failed")

    def test_hold_is_taken_before_anything_reaches_dutchie(self):
        """Minting first and failing to hold would leave a live coupon with no
        ledger backing — free money. The hold must come first."""
        card = self._card(10.0)
        order = []
        T = type(card)
        real_hold = T.hold

        def spy_hold(self, *a, **kw):
            order.append("hold")
            return real_hold(self, *a, **kw)

        def spy_mint(self, *a, **kw):
            order.append("mint")
            return self.env["mint.discount"].sudo().create({
                "name": "c", "discount_type": "dollar_off_total",
                "application_method": "code", "code": "MINT-GD-ORDER1",
                "dutchie_discount_code": "MINT-GD-ORDER1", "maximum_usage_count": 1,
            }), 1

        with patch.object(T, "read_basket", return_value=cart(grand=5.0)), \
             patch.object(T, "hold", spy_hold), \
             patch.object(T, "_mint_child", spy_mint), \
             patch.object(T, "_apply_child", return_value=(True, None)):
            card.execute_draw(2679, 575, "SHIP-1")

        self.assertEqual(order, ["hold", "mint"])

    def test_replaying_the_same_idempotency_key_does_not_draw_twice(self):
        card = self._card(100.0)
        child = self._fake_child("MINT-GD-IDEM01")
        T = type(card)
        with patch.object(T, "read_basket", return_value=cart(grand=30.0)), \
             patch.object(T, "_mint_child", return_value=(child, 999003)), \
             patch.object(T, "_apply_child", return_value=(True, None)):
            first = card.execute_draw(2679, 575, "SHIP-1", idempotency_key="k1")
            second = card.execute_draw(2679, 575, "SHIP-1", idempotency_key="k1")

        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertEqual(len(card.line_ids), 1, "one draw, not two")
        self.assertEqual(card.balance, 70.0)

    def test_draw_is_refused_when_a_concurrent_hold_ate_the_balance(self):
        """plan_draw and hold read the balance at different moments; the hold
        re-checks under a row lock and must win."""
        card = self._card(50.0)
        T = type(card)
        with patch.object(T, "read_basket", return_value=cart(grand=50.0)), \
             patch.object(T, "hold", side_effect=UserError("Draw of 50.0 exceeds the 0.0 remaining")):
            res = card.execute_draw(2679, 575, "SHIP-1")
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "hold_refused")

    def test_replay_of_a_held_line_does_not_mint_a_second_coupon(self):
        """A retry that lands while the first draw is still held must NOT mint
        again — that would put two live coupons into Dutchie against one
        reservation."""
        card = self._card(100.0)
        child = self._fake_child("MINT-GD-ONCE01")
        T = type(card)
        mint_calls = []

        def counting_mint(self, *a, **kw):
            mint_calls.append(1)
            return child, 999004

        with patch.object(T, "read_basket", return_value=cart(grand=30.0)), \
             patch.object(T, "_mint_child", counting_mint), \
             patch.object(T, "_apply_child", return_value=(True, None)):
            card.execute_draw(2679, 575, "SHIP-1", idempotency_key="retry")
            second = card.execute_draw(2679, 575, "SHIP-1", idempotency_key="retry")

        self.assertEqual(len(mint_calls), 1, "minted exactly once")
        self.assertTrue(second.get("replayed"))
        self.assertEqual(card.balance, 70.0)
