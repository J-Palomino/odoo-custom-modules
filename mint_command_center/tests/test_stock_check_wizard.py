from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged("post_install", "-at_install")
class TestStockCheckWizard(TransactionCase):
    """Regression tests for the PTL Check Stock matching fix (Odoo task #94451).

    The Check Stock wizard (mint.stock.check.wizard) marked most PTL deals
    out_of_stock because it substring-matched the deal's master-category
    *bucket* (e.g. "Vapes", "Pre-Rolls") against Dutchie's *sub-category*
    strings ("Cartridge: Distillate", "Infused Prerolls") — only "Flower"
    coincided. This is a regression from migration 19.0.4.5.6, which converted
    product_category from free-text to a master-bucket Selection.

    These tests pin the fixed _deal_in_stock behavior against hand-built
    inventory payloads mirroring the live mintinvsvc
    /api/locations/{uuid}/inventory item shape (verified 2026-06-02:
    quantity_available is a string, master_category in
    {Flower, Edible, Vape, Concentrate, ...}, category holds the Dutchie
    sub-category, brand_name carries the brand). No network access — the
    wizard method takes the inventory list directly.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Brand = cls.env["mint.brand"]
        cls.Deal = cls.env["mint.ptl.deal"]
        cls.wiz = cls.env["mint.stock.check.wizard"].create({})
        # Punctuation variant on purpose — exercises brand normalization.
        cls.brand_tru = cls.Brand.create({"name": "TRU-Infusion"})
        cls.brand_keef = cls.Brand.create({"name": "Keef"})

    def _deal(self, brand, category):
        # brand_id passed explicitly so _compute_brand_id leaves it alone.
        return self.Deal.create({
            "name": f"{brand.name} test deal",
            "brand_id": brand.id,
            "product_category": category,
        })

    @staticmethod
    def _item(brand_name="Keef", master_category="", category="",
              category_path="", quantity_available="5", product_id=1):
        """One inventory item shaped like the live mintinvsvc response."""
        return {
            "product_id": product_id,
            "brand_name": brand_name,
            "master_category": master_category,
            "category": category,
            "category_path": category_path,
            "quantity_available": quantity_available,
        }

    # ── AC04 + AC01 regression: every master bucket resolves, not just Flower ──

    def test_vapes_bucket_matches_cartridge(self):
        deal = self._deal(self.brand_keef, "Vapes")
        items = [self._item(master_category="Vape", category="Cartridge: Distillate")]
        self.assertTrue(self.wiz._deal_in_stock(deal, items))

    def test_prerolls_bucket_matches_infused_preroll(self):
        # Pre-Rolls has no Dutchie master_category; it lives in the sub-category.
        deal = self._deal(self.brand_keef, "Pre-Rolls")
        items = [self._item(master_category="Flower", category="Infused Prerolls")]
        self.assertTrue(self.wiz._deal_in_stock(deal, items))

    def test_edibles_bucket_matches_gummies(self):
        deal = self._deal(self.brand_keef, "Edibles & Tinctures")
        items = [self._item(master_category="Edible", category="Gummies")]
        self.assertTrue(self.wiz._deal_in_stock(deal, items))

    def test_concentrates_bucket_matches_live_resin(self):
        deal = self._deal(self.brand_keef, "Concentrates & Topicals")
        items = [self._item(master_category="Concentrate", category="Cured Resin")]
        self.assertTrue(self.wiz._deal_in_stock(deal, items))

    def test_bucket_label_not_substring_of_subcategory(self):
        # The old code did `deal.product_category.lower() in item_cat` — "vapes"
        # is not a substring of "cartridge: distillate", so it returned False.
        # The fix matches via MASTER_CATEGORY_PATTERNS fragments instead.
        deal = self._deal(self.brand_keef, "Vapes")
        items = [self._item(master_category="", category="Cartridge: Distillate")]
        self.assertTrue(self.wiz._deal_in_stock(deal, items))

    def test_wrong_bucket_excluded(self):
        deal = self._deal(self.brand_keef, "Vapes")
        items = [self._item(master_category="Edible", category="Gummies")]
        self.assertFalse(self.wiz._deal_in_stock(deal, items))

    # ── AC03: Featured Deals is brand-only (no category gate) ──

    def test_featured_deals_brand_only(self):
        deal = self._deal(self.brand_keef, "Featured Deals")
        items = [self._item(master_category="Edible", category="Anything At All")]
        self.assertTrue(self.wiz._deal_in_stock(deal, items))

    def test_featured_deals_wrong_brand_excluded(self):
        deal = self._deal(self.brand_keef, "Featured Deals")
        items = [self._item(brand_name="Some Other Brand",
                            master_category="Edible", category="Gummies")]
        self.assertFalse(self.wiz._deal_in_stock(deal, items))

    # ── AC06: brand matched on a normalized key (punctuation/spacing-insensitive) ──

    def test_brand_normalization_punctuation(self):
        # Deal brand "TRU-Infusion" must match inventory brand "Tru Infusion".
        deal = self._deal(self.brand_tru, "Edibles & Tinctures")
        items = [self._item(brand_name="Tru Infusion",
                            master_category="Edible", category="Gummies")]
        self.assertTrue(self.wiz._deal_in_stock(deal, items))

    # ── AC05: quantity is string-coerced and aggregated per product ──

    def test_small_string_quantity_counts(self):
        # quantity_available "2" (< the old STOCK_THRESHOLD of 10) must still count.
        deal = self._deal(self.brand_keef, "Vapes")
        items = [self._item(master_category="Vape", category="Cartridge",
                            quantity_available="2")]
        self.assertTrue(self.wiz._deal_in_stock(deal, items))

    def test_quantity_aggregates_per_product(self):
        deal = self._deal(self.brand_keef, "Vapes")
        items = [
            self._item(product_id=7, master_category="Vape",
                       category="Cartridge", quantity_available="0"),
            self._item(product_id=7, master_category="Vape",
                       category="Cartridge", quantity_available="3"),
        ]
        self.assertTrue(self.wiz._deal_in_stock(deal, items))

    # ── AC02: carried-at-zero must read out_of_stock (no false positive) ──

    def test_zero_quantity_is_not_in_stock(self):
        deal = self._deal(self.brand_keef, "Vapes")
        items = [self._item(master_category="Vape", category="Cartridge",
                            quantity_available="0")]
        self.assertFalse(self.wiz._deal_in_stock(deal, items))

    def test_empty_inventory_is_not_in_stock(self):
        deal = self._deal(self.brand_keef, "Vapes")
        self.assertFalse(self.wiz._deal_in_stock(deal, []))
