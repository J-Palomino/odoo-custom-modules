# -*- coding: utf-8 -*-
"""Tests for the spin-to-win prize pool and its double-spend guards.

The prize is drawn at claim time from mint.spin.prize, so this model is where
the money rules live: how many of each prize can ever be awarded, that two
concurrent claims cannot take the same entry, and that one customer cannot
claim twice in a day. None of it can be enforced in the browser.
"""
import psycopg2

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase
from odoo.tools import mute_logger


class TestSpinPrizePool(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Pool = self.env['mint.spin.prize']
        self.partner = self.env['res.partner'].create({'name': 'Spin Tester'})
        self.other = self.env['res.partner'].create({'name': 'Other Tester'})
        self.store = self.env['res.company'].search([('is_dispensary', '=', True)], limit=1)
        self.today = '2026-08-23'
        self.tomorrow = '2026-08-24'
        # Existing rows from other tests/data would skew the counts below.
        self.Pool.search([]).unlink()

    def _seed(self, dist=None, batch='test'):
        return self.Pool.seed_pool(dist or {5: 3, 10: 2}, batch)

    # ── the pool IS the cap ────────────────────────────────────────────────

    def test_seed_creates_the_exact_distribution(self):
        self._seed({5: 3, 10: 2, 30: 1})
        self.assertEqual(self.Pool.search_count([('percent', '=', 5)]), 3)
        self.assertEqual(self.Pool.search_count([('percent', '=', 10)]), 2)
        self.assertEqual(self.Pool.search_count([('percent', '=', 30)]), 1)
        self.assertEqual(self.Pool.available_count(), 6)

    def test_seed_requires_a_batch_label(self):
        with self.assertRaises(UserError):
            self.Pool.seed_pool({5: 1}, '')

    def test_seed_rejects_a_nonsense_distribution(self):
        with self.assertRaises(UserError):
            self.Pool.seed_pool({0: 5}, 'bad')
        with self.assertRaises(UserError):
            self.Pool.seed_pool({}, 'empty')

    def test_no_losing_entries_are_possible(self):
        """A zero/negative prize is refused by a CHECK constraint, so 'every
        spin wins' cannot be broken by a stray write."""
        with self.assertRaises(psycopg2.IntegrityError), mute_logger('odoo.sql_db'):
            self.Pool.create({'percent': 0, 'batch': 'x'})

    def test_pool_exhaustion_raises_rather_than_inventing_a_prize(self):
        self._seed({5: 1})
        self.Pool.claim_next(self.partner, self.today, self.store)
        with self.assertRaises(UserError):
            self.Pool.claim_next(self.other, self.today, self.store)

    def test_claiming_decrements_availability(self):
        self._seed({5: 3})
        before = self.Pool.available_count()
        self.Pool.claim_next(self.partner, self.today, self.store)
        self.assertEqual(self.Pool.available_count(), before - 1)

    # ── guard 1: one customer, one claim per day ───────────────────────────

    def test_second_claim_same_day_returns_the_same_entry(self):
        self._seed({5: 5})
        first, created_first = self.Pool.claim_next(self.partner, self.today, self.store)
        second, created_second = self.Pool.claim_next(self.partner, self.today, self.store)
        self.assertTrue(created_first)
        self.assertFalse(created_second, 'a repeat claim must not draw again')
        self.assertEqual(first.id, second.id)

    def test_repeat_claim_consumes_no_inventory(self):
        self._seed({5: 5})
        self.Pool.claim_next(self.partner, self.today, self.store)
        after_first = self.Pool.available_count()
        self.Pool.claim_next(self.partner, self.today, self.store)
        self.assertEqual(self.Pool.available_count(), after_first)

    def test_unique_constraint_is_the_real_guard(self):
        """The search-then-create check is a TOCTOU race; this constraint is
        what actually holds when two requests interleave."""
        self._seed({5: 5})
        entry, _ = self.Pool.claim_next(self.partner, self.today, self.store)
        other_entry = self.Pool.search([('state', '=', 'available')], limit=1)
        with self.assertRaises(psycopg2.IntegrityError), mute_logger('odoo.sql_db'):
            other_entry.write({
                'claimed_partner_id': self.partner.id,
                'claim_date': self.today,
            })

    def test_unclaimed_entries_do_not_collide(self):
        """Postgres treats NULLs as distinct, so many unclaimed rows coexist
        under a unique(partner, date) constraint."""
        self._seed({5: 25})
        self.assertEqual(self.Pool.available_count(), 25)

    def test_next_day_can_claim_again(self):
        self._seed({5: 5})
        today_entry, _ = self.Pool.claim_next(self.partner, self.today, self.store)
        tomorrow_entry, created = self.Pool.claim_next(self.partner, self.tomorrow, self.store)
        self.assertTrue(created)
        self.assertNotEqual(today_entry.id, tomorrow_entry.id)

    def test_two_customers_get_two_entries(self):
        self._seed({5: 5})
        mine, _ = self.Pool.claim_next(self.partner, self.today, self.store)
        theirs, _ = self.Pool.claim_next(self.other, self.today, self.store)
        self.assertNotEqual(mine.id, theirs.id)

    # ── guard 2: two claims never draw the same entry ──────────────────────

    def test_draw_locks_the_row_it_returns(self):
        """_draw_available_id uses FOR UPDATE SKIP LOCKED, so a second draw in
        a concurrent transaction gets a different row. Single-transaction
        stand-in: consecutive draws inside one claim cycle never repeat."""
        self._seed({5: 10})
        drawn = set()
        for i in range(5):
            partner = self.env['res.partner'].create({'name': 'P%s' % i})
            entry, created = self.Pool.claim_next(partner, self.today, self.store)
            self.assertTrue(created)
            self.assertNotIn(entry.id, drawn, 'the same entry was drawn twice')
            drawn.add(entry.id)

    def test_claimed_entries_are_not_redrawn(self):
        self._seed({5: 2})
        a, _ = self.Pool.claim_next(self.partner, self.today, self.store)
        b, _ = self.Pool.claim_next(self.other, self.today, self.store)
        self.assertNotEqual(a.id, b.id)
        self.assertEqual(self.Pool.available_count(), 0)

    # ── the claim's side effects ───────────────────────────────────────────

    def test_claim_marks_the_entry_and_links_the_coupon(self):
        self._seed({15: 1})
        entry, _ = self.Pool.claim_next(self.partner, self.today, self.store)
        self.assertEqual(entry.state, 'claimed')
        self.assertEqual(entry.claimed_partner_id, self.partner)
        self.assertTrue(entry.claimed_at)
        self.assertTrue(entry.discount_id, 'a claim must issue a coupon')

    def test_coupon_matches_the_drawn_percent(self):
        self._seed({25: 1})
        entry, _ = self.Pool.claim_next(self.partner, self.today, self.store)
        self.assertEqual(entry.percent, 25)
        self.assertEqual(entry.discount_id.discount_percent, 25)

    def test_coupon_is_a_percent_discount_not_a_loyalty_redemption(self):
        """loyalty_redemption pushes to Dutchie as 100%-off-one-product, which
        is the wrong reward shape for percent-off-an-order."""
        self._seed({10: 1})
        entry, _ = self.Pool.claim_next(self.partner, self.today, self.store)
        self.assertEqual(entry.discount_id.discount_type, 'percent')
        self.assertEqual(entry.discount_id.redemption_points_cost, 0)

    def test_coupon_code_is_in_both_fields(self):
        """Letting redemption_code and dutchie_discount_code diverge is what
        made every code fail at the register before."""
        self._seed({10: 1})
        entry, _ = self.Pool.claim_next(self.partner, self.today, self.store)
        d = entry.discount_id
        self.assertTrue(d.redemption_code)
        self.assertEqual(d.redemption_code, d.dutchie_discount_code)
        self.assertEqual(d.application_method, 'code')

    def test_coupon_is_single_use_bound_and_expiring(self):
        self._seed({10: 1})
        entry, _ = self.Pool.claim_next(self.partner, self.today, self.store)
        d = entry.discount_id
        self.assertEqual(d.max_redemptions, 1)
        self.assertEqual(d.redemption_limit, 1)
        self.assertEqual(d.maximum_usage_count, 1)
        self.assertEqual(d.redemption_partner_id, self.partner)
        self.assertTrue(d.expires_at)
        self.assertEqual(d.redemption_status, 'pending')

    def test_coupon_is_region_locked(self):
        self._seed({10: 1})
        entry, _ = self.Pool.claim_next(self.partner, self.today, self.store)
        self.assertTrue(entry.discount_id.store_ids,
                        'an unlocked coupon leaks across markets')

    def test_coupon_carries_no_product_scope(self):
        """Order-level discount: scoping it to products would silently stop it
        applying to the rest of the basket."""
        self._seed({10: 1})
        entry, _ = self.Pool.claim_next(self.partner, self.today, self.store)
        self.assertFalse(entry.discount_id.product_ids)

    def test_claim_requires_a_partner_and_a_date(self):
        self._seed({5: 2})
        with self.assertRaises(UserError):
            self.Pool.claim_next(None, self.today, self.store)
        with self.assertRaises(UserError):
            self.Pool.claim_next(self.partner, None, self.store)
