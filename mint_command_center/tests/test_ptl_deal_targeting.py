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


@tagged("post_install", "-at_install")
class TestPtlDealExplicitProducts(MintPtlDealCommon):
    """Locks the explicit-product-picker contract added for Odoo task #93642:
    when `explicit_product_ids` is populated the deal targets ONLY those SKUs
    (intersected with brand_id) and forwards them to mint.discount.product_ids;
    when empty it falls back to the brand+category+excluded_skus matching that
    TestPtlDealTargeting covers.
    """

    def _make_deal(self, **overrides):
        vals = {
            "name": "FA-93642 Explicit Deal",
            "brand_id": self.brand.id,
            "product_category": "Flower",
            "discount_type": "percent",
            "discount_value": 0.2,
        }
        vals.update(overrides)
        return self.PtlDeal.create(vals)

    def test_explicit_products_override_brand_category(self):
        """A populated explicit set wins over brand+category widening."""
        picks = self.flowers[:2]
        deal = self._make_deal(explicit_product_ids=[(6, 0, picks.ids)])
        self.assertEqual(deal.matching_product_count, 2)
        self.assertEqual(set(deal.matching_product_ids.ids), set(picks.ids))
        # The other 3 brand flowers and all vapes must be untouched.
        self.assertFalse(deal.matching_product_ids & (self.flowers[2:] | self.vapes))

    def test_explicit_filters_stale_brand_picks(self):
        """Picks whose brand != the deal's brand are dropped (no leak)."""
        picks = self.flowers[:1] | self.other_flowers[:1]
        deal = self._make_deal(explicit_product_ids=[(6, 0, picks.ids)])
        # Only the in-brand pick survives.
        self.assertEqual(set(deal.matching_product_ids.ids), set(self.flowers[:1].ids))
        self.assertFalse(deal.matching_product_ids & self.other_flowers)

    def test_empty_explicit_falls_back_to_brand_category(self):
        """Empty explicit set keeps today's brand+category matching."""
        deal = self._make_deal()
        self.assertEqual(set(deal.matching_product_ids.ids), set(self.flowers.ids))

    def test_discount_vals_forwards_product_ids_when_explicit(self):
        """_deal_to_discount_vals forwards explicit picks to product_ids so the
        Dutchie discount restricts at the SKU level (Phase 4)."""
        picks = self.flowers[:2]
        deal = self._make_deal(explicit_product_ids=[(6, 0, picks.ids)])
        vals = self.PtlDay._deal_to_discount_vals(deal)
        self.assertIn("product_ids", vals)
        self.assertEqual(set(vals["product_ids"][0][2]), set(picks.ids))

    def test_discount_vals_no_product_ids_without_explicit(self):
        """No explicit picks → no product_ids emitted (back-compat guard)."""
        deal = self._make_deal()
        vals = self.PtlDay._deal_to_discount_vals(deal)
        self.assertNotIn("product_ids", vals)


@tagged("post_install", "-at_install")
class TestPtlDealMarketScoping(MintPtlDealCommon):
    """Market scoping for matching_product_ids (Odoo #587).

    A PTL deal must only fan out across products carried by its OWN market.
    Each product.template row belongs to exactly one Dutchie location
    (x_location_id == res.company.x_dutchie_store_id), so the matcher restricts
    to the deal's store_ids (else its region's stores). This locks in the fix
    for the chatter report on mint.ptl.deal #13955: an AZ "Timeless - Vapes"
    deal was pulling up an MO SKU (44471020) of the same brand.

    matching_product_ids is a preview/format-key field, not the publish path
    (_deal_to_discount_vals already store-scopes the mint.discount); this makes
    the curator's previewed set agree with that store scope.
    """

    AZ_GUID = "FA587-GUID-AZ-0001"
    MO_GUID = "FA587-GUID-MO-0001"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Company = cls.env["res.company"]
        Template = cls.env["product.template"]
        Region = cls.env["mint.region"]

        cls.region_az = Region.create({"name": "FA-587 AZ", "code": "AZ7"})
        cls.az_store = Company.create({
            "name": "FA-587 AZ Store",
            "x_dutchie_store_id": cls.AZ_GUID,
            "region_id": cls.region_az.id,
        })
        cls.mo_store = Company.create({
            "name": "FA-587 MO Store",
            "x_dutchie_store_id": cls.MO_GUID,
        })

        def _mk(name, code, loc):
            return Template.create({
                "name": name,
                "brand_id": cls.brand.id,
                "categ_id": cls.cat_flower.id,
                "default_code": code,
                "type": "consu",
                "x_location_id": loc,
            })

        # Same brand + category, split across markets. The MO rows are the
        # cross-market leak the fix must exclude from an AZ deal.
        cls.az_flowers = Template
        for i in range(3):
            cls.az_flowers |= _mk(f"AZ Flower {i}", f"FA587-AZ-{i}", cls.AZ_GUID)
        cls.mo_flowers = Template
        for i in range(2):
            cls.mo_flowers |= _mk(f"MO Flower {i}", f"FA587-MO-{i}", cls.MO_GUID)
        # In-brand flower with no location — unattributable to any market, so
        # excluded once scoping is active (the 0.1% blank-x_location_id tail).
        cls.blank_flower = _mk("Blank Flower", "FA587-BLANK", False)

    def _make_deal(self, **overrides):
        vals = {
            "name": "FA-587 Deal",
            "brand_id": self.brand.id,
            "product_category": "Flower",
            "discount_type": "percent",
            "discount_value": 0.5,
        }
        vals.update(overrides)
        return self.PtlDeal.create(vals)

    def test_store_ids_scope_excludes_other_market_skus(self):
        """store_ids = AZ store → only AZ-located brand flowers match."""
        deal = self._make_deal(store_ids=[(6, 0, self.az_store.ids)])
        self.assertEqual(set(deal.matching_product_ids.ids), set(self.az_flowers.ids))
        self.assertEqual(deal.matching_product_count, 3)
        # The MO SKUs of the same brand+category must NOT leak in (#587).
        self.assertFalse(deal.matching_product_ids & self.mo_flowers)
        # Blank-location and the common (no-location) flowers are unattributable
        # and must not appear under an explicit store scope.
        self.assertNotIn(self.blank_flower, deal.matching_product_ids)
        self.assertFalse(deal.matching_product_ids & self.flowers)

    def test_region_stores_scope_when_no_store_ids(self):
        """No store_ids → fall back to the deal's region (market_id) stores."""
        deal = self._make_deal(market_id=self.region_az.id)
        self.assertFalse(deal.store_ids)
        self.assertEqual(set(deal.matching_product_ids.ids), set(self.az_flowers.ids))
        self.assertFalse(deal.matching_product_ids & self.mo_flowers)

    def test_market_blind_fallback_when_no_stores_or_region(self):
        """No store_ids and no market_id → unscoped brand+category matching
        (documents the logged market-blind fallback)."""
        deal = self._make_deal()
        self.assertFalse(deal.store_ids)
        self.assertFalse(deal.market_id)
        expected = self.flowers | self.az_flowers | self.mo_flowers | self.blank_flower
        self.assertEqual(set(deal.matching_product_ids.ids), set(expected.ids))

    def test_explicit_picks_are_market_scoped(self):
        """A wrong-market SKU can't be hand-picked past the scope either."""
        picks = self.az_flowers | self.mo_flowers
        deal = self._make_deal(
            store_ids=[(6, 0, self.az_store.ids)],
            explicit_product_ids=[(6, 0, picks.ids)],
        )
        self.assertEqual(set(deal.matching_product_ids.ids), set(self.az_flowers.ids))
        self.assertEqual(deal.matching_product_count, 3)
        self.assertFalse(deal.matching_product_ids & self.mo_flowers)


@tagged("post_install", "-at_install")
class TestPtlWebhookBrandGuard(MintPtlDealCommon):
    """Fail-closed brand guard in `_discount_to_webhook_payload` (Odoo #587 / #90738).

    A PTL deal whose brand cannot resolve to a Dutchie brand id must NOT publish
    with its category scope alone — that swept a "Dr. Zodiak's Moonrock 50% Off"
    (brand=Nirvana, no Dutchie id) onto a $5 MCG preroll. The guard drops the
    category scope so the deal matches nothing until the brand is corrected.
    """

    def _make_deal(self, **overrides):
        vals = {
            "name": "FA-90738 Deal",
            "brand_id": self.brand.id,
            "product_category": "Flower",
            "discount_type": "percent",
            "discount_value": 0.2,
        }
        vals.update(overrides)
        return self.PtlDeal.create(vals)

    def _discount_from(self, deal):
        vals = self.PtlDay._deal_to_discount_vals(deal)
        vals["source"] = "ptl"
        return self.env["mint.discount"].create(vals)

    def test_resolvable_brand_emits_brand_and_category(self):
        """Control: a brand WITH a Dutchie id publishes brand + category."""
        self.cat_flower.dutchie_category_id = "88001"
        self.brand.dutchie_brand_id = "54905"
        payload = self.PtlDay._discount_to_webhook_payload(
            self._discount_from(self._make_deal()), "store-uuid")
        self.assertIsNotNone(payload["brands"])
        self.assertEqual(payload["brands"]["ids"], [54905])
        self.assertIsNotNone(payload["product_categories"])

    def test_unresolvable_brand_suppresses_category_scope(self):
        """Brand WITHOUT a Dutchie id → no brands AND category suppressed."""
        self.cat_flower.dutchie_category_id = "88001"
        self.assertFalse(self.brand.dutchie_brand_id)
        payload = self.PtlDay._discount_to_webhook_payload(
            self._discount_from(self._make_deal()), "store-uuid")
        self.assertIsNone(payload["brands"])
        self.assertIsNone(
            payload["product_categories"],
            "category-only scope must be suppressed when the brand is unresolved")

    def test_unresolvable_brand_keeps_explicit_products(self):
        """An explicit, resolved product list survives the guard."""
        self.brand.dutchie_brand_id = False
        for i, tmpl in enumerate(self.flowers):
            tmpl.dutchie_product_id = str(70000 + i)
        deal = self._make_deal(explicit_product_ids=[(6, 0, self.flowers.ids)])
        payload = self.PtlDay._discount_to_webhook_payload(
            self._discount_from(deal), "store-uuid")
        self.assertIsNone(payload["brands"])
        self.assertIsNotNone(payload["products"])
        self.assertEqual(set(payload["products"]["ids"]), {70000 + i for i in range(5)})
