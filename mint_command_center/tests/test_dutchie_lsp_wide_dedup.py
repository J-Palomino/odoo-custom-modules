"""Regression tests for the LSP-wide duplicate-discount fix in dutchie_publish.py.

Dutchie discounts are LSP-scoped: a created record comes back LocId=0 and applies
to every store in the LSP regardless of which loc it was POSTed under. The old
publisher looped once per configured LocId, producing N identical LSP-wide
duplicates per deal and silently dropping store-subset scoping.

These cover the two pure helpers that carry the fix:
  * _dutchie_loc_restrictions — store_ids -> LocationRestrictions (the scope)
  * _normalize_published_map  — idempotency map parse + legacy migration
"""
from odoo.tests import TransactionCase, tagged
from odoo.exceptions import UserError


@tagged("post_install", "-at_install")
class TestDutchieLspWideDedup(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Submission = cls.env["mint.deal.submission"]
        Company = cls.env["res.company"]
        # Three stores with POS LocIds + one without, to exercise scope + drop.
        cls.store_a = Company.create({"name": "DedupTest Tempe", "dutchie_pos_location_id": 1568})
        cls.store_b = Company.create({"name": "DedupTest Mesa", "dutchie_pos_location_id": 1571})
        cls.store_c = Company.create({"name": "DedupTest Bell", "dutchie_pos_location_id": 1570})
        cls.store_nopos = Company.create({"name": "DedupTest NoPos"})  # no dutchie_pos_location_id

    # ── _dutchie_loc_restrictions ────────────────────────────────────────

    def test_no_stores_is_lsp_wide(self):
        """No store scope -> empty list = LSP-wide (one record for the market)."""
        warns = []
        self.assertEqual(
            self.Submission._dutchie_loc_restrictions(self.env["res.company"], warns), [])
        self.assertEqual(warns, [])

    def test_subset_scopes_to_sorted_pos_loc_ids(self):
        """A store subset -> its POS LocIds, sorted and de-duped."""
        warns = []
        stores = self.store_b | self.store_a | self.store_c  # unsorted union
        self.assertEqual(
            self.Submission._dutchie_loc_restrictions(stores, warns), [1568, 1570, 1571])
        self.assertEqual(warns, [])

    def test_store_without_pos_id_is_dropped_with_warning(self):
        """Stores missing a POS LocId are dropped from scope, but warned about."""
        warns = []
        stores = self.store_a | self.store_nopos
        self.assertEqual(
            self.Submission._dutchie_loc_restrictions(stores, warns), [1568])
        self.assertEqual(len(warns), 1)
        self.assertIn("without a Dutchie POS LocId", warns[0])

    def test_all_stores_missing_pos_id_raises(self):
        """A store-scoped submission where NONE resolve must NOT silently widen
        to the whole market — it raises instead of publishing []."""
        warns = []
        with self.assertRaises(UserError):
            self.Submission._dutchie_loc_restrictions(self.store_nopos, warns)

    # ── _normalize_published_map ─────────────────────────────────────────

    def test_map_empty_and_garbage(self):
        norm = self.Submission._normalize_published_map
        self.assertEqual(norm("", 1568, 395), {})
        self.assertEqual(norm(None, 1568, 395), {})
        self.assertEqual(norm("not json", 1568, 395), {})
        self.assertEqual(norm("{}", 1568, 395), {})

    def test_map_current_shape_unchanged(self):
        """Current {externalId: dutchieId} passes through untouched."""
        norm = self.Submission._normalize_published_map
        self.assertEqual(
            norm('{"lgm_395": 383791, "lgm_395_w2": 383850}', 1568, 395),
            {"lgm_395": 383791, "lgm_395_w2": 383850})

    def test_map_nested_collapses_to_ctx_loc_id(self):
        """Legacy nested {ext: {loc: id}} collapses to the context loc's id —
        this is the exact shape that prod sub 395/396 carried (9 per-loc ids)."""
        norm = self.Submission._normalize_published_map
        raw = ('{"lgm_395": {"1568": 383791, "1570": 383792, "1571": 383793, '
               '"2272": 383794, "2725": 383799}}')
        self.assertEqual(norm(raw, 1568, 395), {"lgm_395": 383791})
        # When ctx_loc isn't in the map, fall back to the first recorded id.
        self.assertEqual(norm(raw, 9999, 395), {"lgm_395": 383791})

    def test_map_oldest_flat_wrapped_under_external_id(self):
        """Oldest flat {loc: id} (no span key) wraps under lgm_<sub> then collapses."""
        norm = self.Submission._normalize_published_map
        self.assertEqual(
            norm('{"1568": 383791, "1571": 383793}', 1568, 395),
            {"lgm_395": 383791})

    def test_map_multi_span_nested_each_collapses(self):
        norm = self.Submission._normalize_published_map
        raw = ('{"lgm_388_w1": {"1568": 100, "1571": 101}, '
               '"lgm_388_w2": {"1568": 200, "1571": 201}}')
        self.assertEqual(norm(raw, 1568, 388), {"lgm_388_w1": 100, "lgm_388_w2": 200})
