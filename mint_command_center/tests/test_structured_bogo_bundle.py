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
