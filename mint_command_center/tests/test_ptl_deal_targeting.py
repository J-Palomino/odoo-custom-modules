from odoo.tests import tagged

from .common import MintPtlDealCommon


@tagged("post_install", "-at_install")
class TestPtlDealTargeting(MintPtlDealCommon):
    """Regression tests for the rule documented in Odoo task #93642:
    PTL deals fan out only across products resolved from
    brand_id + product_category (master bucket) − excluded_skus.

    There is no explicit product multi-select on mint.ptl.deal in prod
    (verified via fields_get on letsgomint.us, module v19.0.4.11.3),
    so these tests lock in the computed-matching behavior.
    """

    def _make_deal(self, **overrides):
        vals = {
            "name": "FA-93642 Deal",
            "brand_id": self.brand.id,
            "product_category": "Flower",
            "discount_type": "percent",
            "discount_value": 0.2,
        }
        vals.update(overrides)
        return self.PtlDeal.create(vals)

    def test_matching_resolves_from_brand_and_category(self):
        """matching_product_ids contains the brand's flower SKUs only."""
        deal = self._make_deal()
        self.assertEqual(deal.matching_product_count, 5)
        self.assertEqual(set(deal.matching_product_ids.ids), set(self.flowers.ids))
        # Vape SKUs from the same brand must NOT appear (category filter works).
        self.assertFalse(deal.matching_product_ids & self.vapes)
        # Other-brand flowers must NOT appear (brand filter works).
        self.assertFalse(deal.matching_product_ids & self.other_flowers)

    def test_excluded_skus_subtracts_from_matching(self):
        """Listing default_codes in excluded_skus removes them from the match."""
        deal = self._make_deal(excluded_skus="FA-FLW-0\nFA-FLW-1\nFA-FLW-2")
        self.assertEqual(deal.matching_product_count, 2)
        remaining_codes = set(deal.matching_product_ids.mapped("default_code"))
        self.assertEqual(remaining_codes, {"FA-FLW-3", "FA-FLW-4"})

    def test_brand_only_no_widening_to_other_brands(self):
        """Empty product_category — match all of brand, never other brands."""
        deal = self._make_deal(product_category=False)
        self.assertEqual(
            set(deal.matching_product_ids.ids),
            set((self.flowers | self.vapes).ids),
        )
        self.assertFalse(deal.matching_product_ids & self.other_flowers)

    def test_discount_vals_emits_brand_and_category_not_product_ids(self):
        """_deal_to_discount_vals does NOT set product_ids today — the
        Dutchie discount filters by brand_ids + category_ids + excluded_skus.
        If a future change adds explicit product targeting, this assertion
        must be re-evaluated against task #93642.
        """
        deal = self._make_deal(excluded_skus="FA-FLW-0")
        vals = self.PtlDay._deal_to_discount_vals(deal)
        self.assertNotIn(
            "product_ids", vals,
            "Prod model has no explicit-SKU multi-select; product_ids must not be emitted.",
        )
        # Brand restriction goes through
        self.assertIn("brand_ids", vals)
        self.assertEqual(vals["brand_ids"], [(6, 0, [self.brand.id])])
        # Category restriction goes through (master bucket expansion)
        self.assertIn("category_ids", vals)
        self.assertIn(self.cat_flower.id, vals["category_ids"][0][2])
        # Exclusion text is forwarded verbatim
        self.assertEqual(vals["excluded_skus"], "FA-FLW-0")
