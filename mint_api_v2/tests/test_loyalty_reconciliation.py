# -*- coding: utf-8 -*-
"""The loyalty balance recompute, and the guards that make it safe.

Regression cover for Odoo #106621. The live run rewrote 81,976 cards
(10,969,001 -> 6,539,534 points, ratio 1.678x -> 1.000x), so the guards matter
more than the arithmetic: each one below prevented real damage.

Every test uses dry_run where a write is not the subject, so a failure here
never depends on mutation succeeding.
"""
from odoo import fields
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


# These fixtures need models from modules mint_api_v2 does NOT depend on
# (mint.dutchie.purchase from mint_dutchie_sync; the day-of-week fields
# create_redemption writes come from mint_command_center). Odoo runs a
# module's tests immediately after that module loads, so at_install would
# execute before those modules are in the registry. post_install defers
# until the whole registry is built.
@tagged('post_install', '-at_install')
class TestLoyaltyReconciliation(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Card = self.env['loyalty.card']
        self.program = self.env['loyalty.program'].search(
            [('program_type', '=', 'loyalty')], limit=1)
        self.assertTrue(self.program)
        self.company = self.env.company

    def _partner(self, name):
        return self.env['res.partner'].create({
            'name': name, 'email': '%s@example.test' % name.replace(' ', '-').lower()})

    def _card(self, partner, points):
        return self.Card.create({
            'partner_id': partner.id,
            'program_id': self.program.id,
            'points': points,
        })

    def _purchase(self, partner, points, receipt):
        return self.env['mint.dutchie.purchase'].create({
            'partner_id': partner.id,
            'loyalty_points': points,
            'receipt_no': receipt,
            'net_total': points,
            # company_id is NOT NULL (and scopes UNIQUE(receipt_no, company_id));
            # date is required=True on the model.
            'company_id': self.env.company.id,
            'date': fields.Date.today(),
        })

    def _set_points(self, card, points):
        """Force the card to a known balance AFTER purchases exist.

        mint.dutchie.purchase.create() awards points atomically (it is the
        single earn source), so creating a fixture purchase moves the card.
        Tests that care about the card's value must pin it afterwards, or they
        assert against whatever the award happened to leave behind.
        """
        card.points = points
        return card

    # --- the formula -----------------------------------------------------

    def test_recompute_equals_sum_of_purchases(self):
        p = self._partner('ZZ Recon Clean')
        card = self._card(p, 999)               # inflated
        self._purchase(p, 30, 'ZZ-RECON-1')
        self._purchase(p, 45, 'ZZ-RECON-2')
        self.assertEqual(card.mint_recompute_balance(), 75)

    def test_recompute_subtracts_live_redemptions(self):
        p = self._partner('ZZ Recon Redeemed')
        card = self._card(p, 500)
        self._purchase(p, 100, 'ZZ-RECON-3')
        product = self.env['product.template'].create({
            'name': 'ZZ Recon Product',
            'x_is_loyalty_redeemable': True,
            'x_loyalty_points_cost': 40,
        })
        self.env['mint.discount'].create_redemption(
            partner=p, points_cost=40, product=product)
        self.assertEqual(card.mint_recompute_balance(), 60)

    def test_voided_redemptions_are_not_deducted(self):
        """A voided redemption is already refunded — deducting it double-counts."""
        p = self._partner('ZZ Recon Voided')
        card = self._card(p, 500)
        self._purchase(p, 100, 'ZZ-RECON-4')
        product = self.env['product.template'].create({
            'name': 'ZZ Recon Voided Product',
            'x_is_loyalty_redeemable': True,
            'x_loyalty_points_cost': 40,
        })
        redemption = self.env['mint.discount'].create_redemption(
            partner=p, points_cost=40, product=product)
        redemption.action_void_redemption()
        self.assertEqual(card.mint_recompute_balance(), 100)

    # --- the guards ------------------------------------------------------

    def test_card_without_purchases_is_left_alone(self):
        """No ground truth => no proof the balance is wrong. Never zero it.

        This is the guard that saved a real customer holding 1,297 points from
        a non-purchase source during the live run.
        """
        p = self._partner('ZZ Recon No Truth')
        card = self._card(p, 1297)
        self.assertIsNone(card.mint_recompute_balance())
        stats = self.Card.mint_reconcile_balances(dry_run=True)
        self.assertNotIn(card.id, [c[0] for c in stats['changes']])
        self.assertEqual(card.points, 1297)

    def test_under_credited_card_is_never_topped_up(self):
        """The recompute only removes inflation; it does not grant points."""
        p = self._partner('ZZ Recon Under')
        card = self._card(p, 0)
        self._purchase(p, 100, 'ZZ-RECON-5')
        self._set_points(card, 20)          # under-credited relative to purchases
        self.assertEqual(card.mint_recompute_balance(), 100)
        stats = self.Card.mint_reconcile_balances(dry_run=True)
        self.assertNotIn(card.id, [c[0] for c in stats['changes']])

    def test_negative_result_is_never_written(self):
        """Redemptions exceeding earnings must not produce a negative balance."""
        p = self._partner('ZZ Recon Negative')
        card = self._card(p, 0)
        self._purchase(p, 10, 'ZZ-RECON-6')
        self._set_points(card, 10)
        product = self.env['product.template'].create({
            'name': 'ZZ Recon Neg Product',
            'x_is_loyalty_redeemable': True,
            'x_loyalty_points_cost': 50,
        })
        self.env['mint.discount'].create_redemption(
            partner=p, points_cost=50, product=product)
        self.assertEqual(card.mint_recompute_balance(), -40)
        stats = self.Card.mint_reconcile_balances(dry_run=True)
        self.assertNotIn(card.id, [c[0] for c in stats['changes']])

    # --- dry-run contract ------------------------------------------------

    def test_dry_run_reports_without_writing(self):
        p = self._partner('ZZ Recon DryRun')
        card = self._card(p, 0)
        self._purchase(p, 100, 'ZZ-RECON-7')
        self._set_points(card, 400)         # simulate the inflated state
        stats = self.Card.mint_reconcile_balances(dry_run=True)

        self.assertIn((card.id, 400.0, 100.0), stats['changes'])
        self.assertEqual(stats['corrected'], 0, "dry run must not write")
        self.assertEqual(card.points, 400, "dry run must not write")

    def test_apply_writes_the_recomputed_balance(self):
        p = self._partner('ZZ Recon Apply')
        card = self._card(p, 0)
        self._purchase(p, 100, 'ZZ-RECON-8')
        self._set_points(card, 400)         # simulate the inflated state

        self.Card.mint_reconcile_balances(dry_run=False)

        self.assertEqual(card.points, 100)

    def test_apply_is_idempotent(self):
        p = self._partner('ZZ Recon Idempotent')
        card = self._card(p, 0)
        self._purchase(p, 100, 'ZZ-RECON-9')
        self._set_points(card, 400)         # simulate the inflated state

        self.Card.mint_reconcile_balances(dry_run=False)
        second = self.Card.mint_reconcile_balances(dry_run=False)

        self.assertEqual(card.points, 100)
        self.assertNotIn(card.id, [c[0] for c in second['changes']],
                         "a reconciled card must not be touched again")
