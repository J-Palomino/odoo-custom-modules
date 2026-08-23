# -*- coding: utf-8 -*-
"""Tests for spin-to-win coupon minting.

The wheel is played anonymously and the prize is only tied to a customer at
claim time, so this model method is the single point where the money rules are
enforced: which percentage the prize is actually worth, one claim per customer
per day, and idempotency under a double-submit. All three are asserted here
because none of them can be enforced in the browser.
"""
from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase


class TestSpinRedemption(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Discount = self.env['mint.discount']
        self.partner = self.env['res.partner'].create({'name': 'Spin Tester'})
        self.other_partner = self.env['res.partner'].create({'name': 'Other Tester'})
        self.store = self.env['res.company'].search([('is_dispensary', '=', True)], limit=1)
        self.today = '2026-08-23'

    def _ext(self, partner=None, date=None, nonce='n1'):
        partner = partner or self.partner
        return 'lgm_spin_%s_%s_%s' % (date or self.today, partner.id, nonce)

    def _claim(self, prize_id='off-15', partner=None, date=None, nonce='n1'):
        return self.Discount.create_spin_redemption(
            partner=partner or self.partner,
            prize_id=prize_id,
            spin_date=date or self.today,
            external_id=self._ext(partner, date, nonce),
            store=self.store,
        )

    # ── the percentage is decided here, never by the caller ────────────────

    def test_percent_resolved_from_prize_id(self):
        for prize_id, expected in self.Discount.SPIN_PRIZE_PERCENTS.items():
            rec = self._claim(prize_id=prize_id, nonce=prize_id)
            self.assertEqual(rec.discount_percent, expected)
            rec.unlink()

    def test_unknown_prize_is_rejected(self):
        with self.assertRaises(UserError):
            self._claim(prize_id='off-90')

    def test_requires_a_partner(self):
        with self.assertRaises(UserError):
            self.Discount.create_spin_redemption(
                partner=None, prize_id='off-5', spin_date=self.today,
                external_id=self._ext(), store=self.store)

    def test_requires_an_external_id(self):
        with self.assertRaises(UserError):
            self.Discount.create_spin_redemption(
                partner=self.partner, prize_id='off-5', spin_date=self.today,
                external_id='', store=self.store)

    # ── record shape ───────────────────────────────────────────────────────

    def test_is_a_percent_discount_not_a_loyalty_redemption(self):
        """loyalty_redemption pushes to Dutchie as 100%-off-one-product, which
        is the wrong reward shape for percent-off-an-order."""
        rec = self._claim()
        self.assertEqual(rec.discount_type, 'percent')
        self.assertEqual(rec.redemption_points_cost, 0)

    def test_code_is_in_both_fields(self):
        """Letting redemption_code and dutchie_discount_code diverge is what
        made every code fail at the register before."""
        rec = self._claim()
        self.assertTrue(rec.redemption_code)
        self.assertEqual(rec.redemption_code, rec.dutchie_discount_code)
        self.assertEqual(rec.application_method, 'code')

    def test_is_single_use_and_expiring(self):
        rec = self._claim()
        self.assertEqual(rec.max_redemptions, 1)
        self.assertEqual(rec.redemption_limit, 1)
        self.assertEqual(rec.maximum_usage_count, 1)
        self.assertTrue(rec.expires_at)
        self.assertEqual(rec.redemption_status, 'pending')

    def test_is_bound_to_the_claiming_customer(self):
        rec = self._claim()
        self.assertEqual(rec.redemption_partner_id, self.partner)

    def test_name_leads_with_the_code(self):
        """Dutchie truncates the name to 120 chars for OnlineName."""
        rec = self._claim()
        self.assertTrue(rec.name.startswith(rec.redemption_code))

    def test_is_region_locked_to_the_store(self):
        rec = self._claim()
        self.assertTrue(rec.store_ids, 'an unlocked coupon leaks across markets')

    def test_carries_no_product_scope(self):
        """Order-level discount: scoping it to products would silently stop it
        applying to the rest of the basket."""
        rec = self._claim()
        self.assertFalse(rec.product_ids)

    # ── the money rules ────────────────────────────────────────────────────

    def test_same_external_id_is_idempotent(self):
        """A double-submit or retried fetch must not mint a second code."""
        first = self._claim()
        second = self._claim()
        self.assertEqual(first.id, second.id)
        self.assertEqual(first.redemption_code, second.redemption_code)

    def test_second_claim_same_day_returns_the_first(self):
        """Clearing localStorage and spinning again must not yield a 2nd code.
        The controller turns 'returned a different external_id' into a 409."""
        first = self._claim(prize_id='off-5', nonce='n1')
        second = self._claim(prize_id='off-30', nonce='n2')
        self.assertEqual(first.id, second.id)
        self.assertEqual(second.discount_percent, 5, 'must not upgrade the prize')

    def test_next_day_can_claim_again(self):
        first = self._claim(date='2026-08-23')
        later = self._claim(date='2026-08-24')
        self.assertNotEqual(first.id, later.id)

    def test_another_customer_is_unaffected(self):
        mine = self._claim()
        theirs = self._claim(partner=self.other_partner)
        self.assertNotEqual(mine.id, theirs.id)
        self.assertEqual(theirs.redemption_partner_id, self.other_partner)

    def test_codes_are_unique_across_claims(self):
        a = self._claim(date='2026-08-23')
        b = self._claim(date='2026-08-24')
        self.assertNotEqual(a.redemption_code, b.redemption_code)
