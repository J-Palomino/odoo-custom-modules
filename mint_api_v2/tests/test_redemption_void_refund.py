# -*- coding: utf-8 -*-
"""Voiding a redemption must refund the points — for PRODUCT redemptions too.

Regression cover for the bug fixed in Odoo #106616.

action_void_redemption used to refund only when redemption_reward_id was set:

    reward = rec.redemption_reward_id
    if reward and rec.redemption_partner_id and rec.redemption_points_cost:
        ...refund...

The live /rewards path calls create_redemption(product=...), which never sets
redemption_reward_id — so voiding a product redemption marked it 'voided' and
SILENTLY KEPT the customer's deducted points. The reward path was fine, which
is why it went unnoticed: the tests below cover both, so a future refactor
cannot fix one and re-break the other.

Idempotency is enforced by the status != 'pending' guard (a voided redemption
cannot be voided again), which is asserted explicitly — a double refund would
mint points out of nothing.
"""
from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


# These fixtures need models from modules mint_api_v2 does NOT depend on
# (mint.dutchie.purchase from mint_dutchie_sync; the day-of-week fields
# create_redemption writes come from mint_command_center). Odoo runs a
# module's tests immediately after that module loads, so at_install would
# execute before those modules are in the registry. post_install defers
# until the whole registry is built.
@tagged('post_install', '-at_install')
class TestRedemptionVoidRefund(TransactionCase):

    def setUp(self):
        super().setUp()
        self.partner = self.env['res.partner'].create({
            'name': 'ZZ Void Refund Customer',
            'email': 'zz-voidrefund-uniq@example.test',
        })
        self.program = self.env['loyalty.program'].search(
            [('program_type', '=', 'loyalty')], limit=1)
        self.assertTrue(
            self.program, "a loyalty-type loyalty.program must exist")
        self.card = self.env['loyalty.card'].create({
            'partner_id': self.partner.id,
            'program_id': self.program.id,
            'points': 500,
        })
        self.product = self.env['product.template'].create({
            'name': 'ZZ Redeemable Test Product',
            'x_is_loyalty_redeemable': True,
            'x_loyalty_points_cost': 100,
        })
        self.Discount = self.env['mint.discount']

    def _redeem_product(self, cost=100):
        """Mimic redeem_loyalty: deduct, then mint the redemption."""
        self.card.points -= cost
        return self.Discount.create_redemption(
            partner=self.partner, points_cost=cost, product=self.product)

    # --- the regression --------------------------------------------------

    def test_voiding_a_product_redemption_refunds_points(self):
        redemption = self._redeem_product(cost=100)
        self.assertEqual(self.card.points, 400)
        self.assertFalse(
            redemption.redemption_reward_id,
            "the product path must not set redemption_reward_id — that is "
            "precisely the condition the old refund gate keyed on")

        redemption.action_void_redemption()

        self.assertEqual(self.card.points, 500, "points must be refunded")
        self.assertEqual(redemption.redemption_status, 'voided')

    def test_voiding_a_reward_redemption_still_refunds(self):
        """No regression on the path that already worked."""
        reward = self.program.reward_ids[:1]
        if not reward:
            self.skipTest('loyalty program has no reward records configured')
        self.card.points -= 100
        redemption = self.Discount.create_redemption(
            partner=self.partner, points_cost=100, reward=reward)
        self.assertEqual(self.card.points, 400)

        redemption.action_void_redemption()

        self.assertEqual(self.card.points, 500)
        self.assertEqual(redemption.redemption_status, 'voided')

    # --- idempotency -----------------------------------------------------

    def test_double_void_cannot_refund_twice(self):
        redemption = self._redeem_product(cost=100)
        redemption.action_void_redemption()
        self.assertEqual(self.card.points, 500)

        with self.assertRaises(UserError):
            redemption.action_void_redemption()

        self.assertEqual(
            self.card.points, 500,
            "a second void must not mint points out of nothing")

    def test_cannot_void_a_used_redemption(self):
        redemption = self._redeem_product(cost=100)
        redemption.action_mark_redemption_used()
        self.assertEqual(redemption.redemption_status, 'used')

        with self.assertRaises(UserError):
            redemption.action_void_redemption()

        self.assertEqual(
            self.card.points, 400,
            "a consumed redemption must not be refundable")

    # --- shape -----------------------------------------------------------

    def test_redemption_carries_a_unique_code_and_expiry(self):
        a = self._redeem_product(cost=100)
        b = self._redeem_product(cost=100)
        self.assertTrue(a.redemption_code and b.redemption_code)
        self.assertNotEqual(a.redemption_code, b.redemption_code)
        self.assertEqual(a.redemption_status, 'pending')
        self.assertTrue(a.expires_at, "redemptions must expire")
