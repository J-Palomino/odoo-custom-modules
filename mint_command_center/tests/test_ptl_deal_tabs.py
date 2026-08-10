from lxml import etree

from odoo.tests import tagged

from .common import MintPtlDealCommon


@tagged("post_install", "-at_install")
class TestPtlDealTabbedConfig(MintPtlDealCommon):
    """Locks the tabbed Inclusion / Exclusion config from Odoo task #93633.

    AC01 - the mint.ptl.deal form renders a notebook with Included / Excluded pages.
    AC02 - the Included page exposes brand_id, product_category, sales_details,
           explicit_product_ids and the resolved matching_product_ids preview.
    AC03 - the Excluded page exposes excluded_brand_ids, excluded_skus,
           details_exclusions.
    AC05 - the resolved list lives on the Included tab (matching_product_ids) and
           honors explicit_product_ids + excluded_skus; excluded_brand_ids is a
           no-op for single-brand matching and there is NO separate Excluded-tab
           preview field (excluded_preview_ids).

    The model-matching behaviour (brand + category - excluded_skus, explicit
    override) is covered by TestPtlDealTargeting; this module covers the view
    arch + the AC05 no-op/absence guarantees that closing #93633 depends on.
    """

    def _form_arch(self):
        view = self.env.ref("mint_command_center.view_ptl_deal_form")
        arch = self.PtlDeal.get_view(view.id, "form")["arch"]
        return etree.fromstring(arch)

    @staticmethod
    def _page(tree, name):
        pages = tree.xpath(f'//page[@name="{name}"]')
        return pages[0] if pages else None

    @staticmethod
    def _fields_in(node):
        return {f.get("name") for f in node.xpath(".//field")}

    # ---- AC01: notebook with Included / Excluded pages -------------------
    def test_form_has_included_and_excluded_notebook_pages(self):
        tree = self._form_arch()
        self.assertTrue(tree.xpath("//notebook"), "deal form has no notebook")
        included = self._page(tree, "included")
        excluded = self._page(tree, "excluded")
        self.assertIsNotNone(included, "Included notebook page missing")
        self.assertIsNotNone(excluded, "Excluded notebook page missing")
        self.assertEqual(included.get("string"), "Included")
        self.assertEqual(excluded.get("string"), "Excluded")

    # ---- AC02: Included page fields --------------------------------------
    def test_included_page_exposes_inclusion_fields(self):
        included = self._page(self._form_arch(), "included")
        fields = self._fields_in(included)
        for expected in (
            "brand_id",
            "product_category",
            "sales_details",
            "explicit_product_ids",
            "matching_product_ids",
        ):
            self.assertIn(expected, fields, f"Included page missing {expected}")

    # ---- AC03: Excluded page fields --------------------------------------
    def test_excluded_page_exposes_exclusion_fields(self):
        excluded = self._page(self._form_arch(), "excluded")
        fields = self._fields_in(excluded)
        for expected in ("excluded_brand_ids", "excluded_skus", "details_exclusions"):
            self.assertIn(expected, fields, f"Excluded page missing {expected}")

    # ---- AC05: resolved preview is the Included tab, not the Excluded one --
    def test_resolved_preview_lives_on_included_tab_only(self):
        tree = self._form_arch()
        included_fields = self._fields_in(self._page(tree, "included"))
        excluded_fields = self._fields_in(self._page(tree, "excluded"))
        self.assertIn("matching_product_ids", included_fields)
        self.assertNotIn(
            "matching_product_ids",
            excluded_fields,
            "Excluded tab must stay inputs-only (no resolved-product list)",
        )

    def test_no_separate_excluded_preview_field(self):
        """AC05-Q2 resolution: the Excluded tab is inputs-only; no excluded_preview_ids."""
        self.assertNotIn(
            "excluded_preview_ids",
            self.PtlDeal._fields,
            "excluded_preview_ids was added — AC05 deliberately keeps the Excluded "
            "tab inputs-only; revisit task #93633 if a separate preview is now wanted.",
        )

    def test_excluded_brand_ids_is_noop_for_single_brand_matching(self):
        """AC05-Q1 resolution: matching is single-brand, so excluding *other*
        brands cannot refine the set. excluded_brand_ids must not change the
        resolved products (and must not be in the compute's @api.depends)."""
        deal = self.PtlDeal.create({
            "name": "AC05 no-op deal",
            "brand_id": self.brand.id,
            "product_category": "Flower",
            "discount_type": "percent",
            "discount_value": 0.2,
        })
        baseline = set(deal.matching_product_ids.ids)
        self.assertEqual(baseline, set(self.flowers.ids))

        # Excluding another brand: provable no-op for a single-brand deal.
        deal.excluded_brand_ids = [(6, 0, self.other_brand.ids)]
        deal.invalidate_recordset(["matching_product_ids", "matching_product_count"])
        self.assertEqual(
            set(deal.matching_product_ids.ids),
            baseline,
            "excluded_brand_ids changed the single-brand match — it should be a no-op",
        )

        # Even excluding the deal's own brand does not refine the set, proving
        # excluded_brand_ids is not wired into _compute_matching_products.
        deal.excluded_brand_ids = [(6, 0, self.brand.ids)]
        deal.invalidate_recordset(["matching_product_ids", "matching_product_count"])
        self.assertEqual(set(deal.matching_product_ids.ids), baseline)

    def test_excluded_skus_still_refines_the_included_preview(self):
        """The one exclusion that *does* filter a single-brand set is
        excluded_skus, and it nets out of the Included-tab resolved list."""
        deal = self.PtlDeal.create({
            "name": "AC05 excluded_skus deal",
            "brand_id": self.brand.id,
            "product_category": "Flower",
            "discount_type": "percent",
            "discount_value": 0.2,
            "excluded_skus": "FA-FLW-0\nFA-FLW-1",
        })
        codes = set(deal.matching_product_ids.mapped("default_code"))
        self.assertEqual(codes, {"FA-FLW-2", "FA-FLW-3", "FA-FLW-4"})
