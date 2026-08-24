"""Structured BOGO / bundle-tier behavior (#93677 Cluster C).

Covers the pure formatters, the submission sales-text generation, the
convert-to-deal carry-over, and the mint.discount mirror mapping
(threshold/amount/calc id reset + structured overrides).
"""
from odoo.tests import TransactionCase, tagged

from ..models.deal_mixins import format_bogo_text, format_bundle_tiers_text


@tagged('post_install', '-at_install')
class TestStructuredBogoBundle(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Submission = cls.env['mint.deal.submission']
        cls.Deal = cls.env['mint.ptl.deal']

    # ── Pure formatters ──────────────────────────────────────────────────

    def test_format_bogo_text(self):
        self.assertEqual(format_bogo_text(1, 1, 1.0), 'B1G1 Free')
        self.assertEqual(format_bogo_text(1, 1, 0.5), 'B1G1 50% Off')
        self.assertEqual(format_bogo_text(2, 1, 1.0), 'B2G1 Free')
        self.assertEqual(format_bogo_text(1, 1, 50), 'B1G1 50% Off')  # whole-pct tolerated
        self.assertEqual(format_bogo_text(0, 1, 0.5), '')             # unset triple

    def test_format_bundle_tiers_text(self):
        self.assertEqual(
            format_bundle_tiers_text([(2, 18.0), (3, 25.0)]),
            '2 for $18 or 3 for $25')
        self.assertEqual(format_bundle_tiers_text([(2, 22.5)]), '2 for $22.50')
        self.assertEqual(format_bundle_tiers_text([(0, 18.0)]), '')  # skips bad rows

    # ── Submission sales text from structure ─────────────────────────────

    def test_submission_bundle_sales_text(self):
        sub = self.Submission.create({
            'name': 'Bundle Test',
            'vendor_name': 'TestVendor',
            'discount_type': 'bundle',
            'bundle_tier_ids': [
                (0, 0, {'sequence': 10, 'qty': 2, 'price': 18.0}),
                (0, 0, {'sequence': 20, 'qty': 3, 'price': 25.0}),
            ],
        })
        self.assertEqual(sub._format_sales_details(), '2 for $18 or 3 for $25')

    def test_submission_bogo_sales_text(self):
        sub = self.Submission.create({
            'name': 'BOGO Test',
            'vendor_name': 'TestVendor',
            'discount_type': 'bogo',
            'bogo_variant': 'b1g1',
            'bogo_buy_qty': 1,
            'bogo_get_qty': 1,
            'bogo_get_pct': 0.5,
        })
        self.assertEqual(sub._format_sales_details(), 'B1G1 50% Off')
        # Legacy bogo without the triple keeps the literal fallback.
        sub_legacy = self.Submission.create({
            'name': 'Legacy BOGO',
            'vendor_name': 'TestVendor',
            'discount_type': 'bogo',
        })
        self.assertEqual(sub_legacy._format_sales_details(), 'BOGO')

    # ── Convert-to-deal carry-over ───────────────────────────────────────

    def test_convert_carries_structure(self):
        sub = self.Submission.create({
            'name': 'Carry Test',
            'vendor_name': 'TestVendor',
            'discount_type': 'bundle',
            'state': 'approved',
            'bundle_tier_ids': [(0, 0, {'qty': 2, 'price': 18.0})],
        })
        sub.with_context(force_override=True).action_convert_to_deal()
        self.assertTrue(sub.deal_id)
        self.assertEqual(
            [(t.qty, t.price) for t in sub.deal_id.bundle_tier_ids],
            [(2, 18.0)])

    # ── Deal display text ────────────────────────────────────────────────

    def test_deal_display_text_bundle_literal(self):
        deal = self.Deal.create({
            'name': 'Bundle Deal',
            'discount_type': 'bundle',
            'bundle_tier_ids': [
                (0, 0, {'sequence': 10, 'qty': 2, 'price': 18.0}),
                (0, 0, {'sequence': 20, 'qty': 3, 'price': 25.0}),
            ],
        })
        self.assertEqual(deal.display_text, '2 for $18 or 3 for $25')

    def test_deal_display_text_bogo_structured(self):
        deal = self.Deal.create({
            'name': 'BOGO Deal',
            'discount_type': 'bogo',
            'bogo_buy_qty': 1,
            'bogo_get_qty': 1,
            'bogo_get_pct': 1.0,
        })
        self.assertEqual(deal.display_text, 'B1G1 Free')

    # ── Discount mirror mapping ──────────────────────────────────────────

    def test_discount_vals_mirror(self):
        Day = self.env['mint.ptl.day']
        has_calc = 'calculation_method_id' in self.env['mint.discount']._fields

        bogo = self.Deal.create({
            'name': 'BOGO Mirror',
            'discount_type': 'bogo',
            'bogo_buy_qty': 1,
            'bogo_get_qty': 1,
            'bogo_get_pct': 0.5,
        })
        vals = Day._deal_to_discount_vals(bogo)
        self.assertEqual(vals['threshold_min'], 2)
        self.assertEqual(vals['discount_amount'], 0.5)

        bundle = self.Deal.create({
            'name': 'Bundle Mirror',
            'discount_type': 'bundle',
            'bundle_tier_ids': [(0, 0, {'qty': 2, 'price': 18.0})],
        })
        vals = Day._deal_to_discount_vals(bundle)
        if has_calc:
            self.assertEqual(vals['threshold_min'], 2)
            self.assertEqual(vals['discount_amount'], 18.0)
            self.assertEqual(vals['calculation_method_id'], 6)
        else:
            # Without the canonical registry field the price must NOT be
            # written into the percent-interpreted amount slot.
            self.assertEqual(vals['threshold_min'], 0)

        # De-structured deal must reset the threshold (stale-write guard).
        plain = self.Deal.create({
            'name': 'Plain Percent',
            'discount_type': 'percent',
            'discount_value': 30.0,
        })
        vals = Day._deal_to_discount_vals(plain)
        self.assertEqual(vals['threshold_min'], 0)

    # ── Dutchie calculation-method parity ────────────────────────────────

    def test_bogo_emits_dutchie_price_to_amount(self):
        """Dutchie has no BOGO method: a BOGO is PRICE_TO_AMOUNT (id 3) with
        threshold_min=buy+get and the "get" item's resulting price."""
        Day = self.env['mint.ptl.day']
        if 'calculation_method_id' not in self.env['mint.discount']._fields:
            self.skipTest('canonical registry field not installed')

        half_off = self.Deal.create({
            'name': 'B1G1 50% Off With Price',
            'discount_type': 'bogo',
            'bogo_buy_qty': 1,
            'bogo_get_qty': 1,
            'bogo_get_pct': 0.5,
            'original_price': 20.0,
        })
        vals = Day._deal_to_discount_vals(half_off)
        self.assertEqual(vals['calculation_method_id'], 3)
        self.assertEqual(vals['threshold_min'], 2)
        self.assertEqual(vals['discount_amount'], 10.0)

    def test_bogo_free_get_uses_penny_convention(self):
        """A free "get" item is $0.01 in Dutchie, never $0.00."""
        Day = self.env['mint.ptl.day']
        if 'calculation_method_id' not in self.env['mint.discount']._fields:
            self.skipTest('canonical registry field not installed')

        free = self.Deal.create({
            'name': 'B1G1 Free With Price',
            'discount_type': 'bogo',
            'bogo_buy_qty': 1,
            'bogo_get_qty': 1,
            'bogo_get_pct': 1.0,
            'original_price': 30.0,
        })
        vals = Day._deal_to_discount_vals(free)
        self.assertEqual(vals['calculation_method_id'], 3)
        self.assertEqual(vals['discount_amount'], 0.01)

    def test_bogo_without_original_price_leaves_method_unset(self):
        """Without original_price the resulting price is unknowable — the
        method stays unset rather than publishing a fabricated one."""
        Day = self.env['mint.ptl.day']
        unpriced = self.Deal.create({
            'name': 'B1G1 No Price',
            'discount_type': 'bogo',
            'bogo_buy_qty': 1,
            'bogo_get_qty': 1,
            'bogo_get_pct': 1.0,
        })
        vals = Day._deal_to_discount_vals(unpriced)
        self.assertEqual(vals['threshold_min'], 2)
        self.assertEqual(vals.get('calculation_method_id', 0), 0)
        self.assertEqual(vals['discount_amount'], 1.0)

    def test_webhook_payload_uses_canonical_calc_method(self):
        """The PTL webhook must emit registry strings, not the retired local
        map's invented ones ('BOGO', 'DOLLAR_OFF', 'POINTS_MULTIPLIER')."""
        Day = self.env['mint.ptl.day']
        Discount = self.env['mint.discount']
        if 'calculation_method_id' not in Discount._fields:
            self.skipTest('canonical registry field not installed')

        disc = Discount.create({
            'name': 'Payload Parity',
            'discount_type': 'bogo',
            'discount_amount': 0.01,
            'calculation_method_id': 3,
        })
        payload = Day._discount_to_webhook_payload(disc, 'loc-uuid')
        self.assertEqual(payload['calculation_method'], 'PRICE_TO_AMOUNT')
        self.assertNotEqual(payload['calculation_method'], 'BOGO')
        self.assertEqual(payload['discount_amount'], 0.01)
