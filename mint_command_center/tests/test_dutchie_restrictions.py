"""Regression: Dutchie discount restriction assembly.

Guards the fix for deal sub 395 ("IO Extracts — 2 for $35"), which published
with Brand + Category + an explicit 239-product include. Dutchie intersects
restriction types, so the extra Product include collapsed eligibility from the
233 products that Brand+Category cover down to 48. The rule under test: send a
Product INCLUDE only when it is the sole scoping signal; when a Brand/Category
already scopes the deal, drop the include (with a warning) and keep any product
EXCLUDES on the slot.

These exercise the pure ``build_dutchie_restrictions`` helper directly (the
same path ``DealSubmissionDutchiePublish._dutchie_build`` uses), so no Dutchie
fixtures are required.
"""
from odoo.tests import TransactionCase, tagged

from ..models.deal_mixins import build_dutchie_restrictions

B = [18992]        # brand include (e.g. IO Extracts @ LSP 575)
EB = [9001]        # brand exclude
PI = [10, 11, 12]  # product include (the over-constraining list)
PE = [20, 21]      # product exclude
C = [23378]        # category include


def _slot(restr, key):
    r = restr[key]
    return (r['IsExclusion'], r['RestrictionIds'])


@tagged('post_install', '-at_install')
class TestDutchieRestrictions(TransactionCase):

    # ── The bug case + its fix ───────────────────────────────────────────
    def test_brand_category_product_drops_include(self):
        """sub 395 shape: brand+category+products -> Product dropped, warned."""
        restr, warns = build_dutchie_restrictions(B, [], PI, [], C)
        self.assertEqual(_slot(restr, 'Brand'), (False, B))
        self.assertEqual(_slot(restr, 'Category'), (False, C))
        self.assertEqual(_slot(restr, 'Product'), (False, []),
                         "Product include must be dropped when Brand+Category scope the deal")
        self.assertTrue(any('product include' in w for w in warns))

    def test_brand_category_keeps_excludes_when_include_dropped(self):
        """brand+category+inc+exc -> include dropped, EXCLUDE now applied."""
        restr, warns = build_dutchie_restrictions(B, [], PI, PE, C)
        self.assertEqual(_slot(restr, 'Product'), (True, PE),
                         "product excludes must survive when the include is dropped")

    def test_brand_only_drops_product_include(self):
        restr, warns = build_dutchie_restrictions(B, [], PI, [], [])
        self.assertEqual(_slot(restr, 'Product'), (False, []))
        self.assertTrue(any('product include' in w for w in warns))

    def test_category_only_drops_product_include(self):
        restr, warns = build_dutchie_restrictions([], [], PI, [], C)
        self.assertEqual(_slot(restr, 'Product'), (False, []))
        self.assertTrue(any('product include' in w for w in warns))

    # ── Unchanged behaviour (no regression) ──────────────────────────────
    def test_product_only_keeps_include(self):
        """No brand/category -> product list IS the scope, keep it."""
        restr, warns = build_dutchie_restrictions([], [], PI, [], [])
        self.assertEqual(_slot(restr, 'Product'), (False, PI))
        self.assertFalse(any('product include' in w for w in warns))

    def test_product_only_with_excludes_warns(self):
        restr, warns = build_dutchie_restrictions([], [], PI, PE, [])
        self.assertEqual(_slot(restr, 'Product'), (False, PI))
        self.assertTrue(any('exclusions dropped' in w for w in warns))

    def test_exclude_only(self):
        restr, warns = build_dutchie_restrictions([], [], [], PE, [])
        self.assertEqual(_slot(restr, 'Product'), (True, PE))

    def test_brand_category_only(self):
        """sub 396 shape: brand+category, no product list -> untouched."""
        restr, warns = build_dutchie_restrictions(B, [], [], [], C)
        self.assertEqual(_slot(restr, 'Brand'), (False, B))
        self.assertEqual(_slot(restr, 'Category'), (False, C))
        self.assertEqual(_slot(restr, 'Product'), (False, []))
        self.assertEqual(warns, [])

    def test_exclude_brand_is_not_positive_scope(self):
        """A brand EXCLUDE doesn't scope a positive set, so a product include
        is still the scope and is kept."""
        restr, warns = build_dutchie_restrictions([], EB, PI, [], [])
        self.assertEqual(_slot(restr, 'Brand'), (True, EB))
        self.assertEqual(_slot(restr, 'Product'), (False, PI))

    def test_no_restrictions_all_empty(self):
        """Nothing resolved -> every slot empty (caller raises store-wide guard)."""
        restr, warns = build_dutchie_restrictions([], [], [], [], [])
        self.assertFalse(any(r['RestrictionIds'] for r in restr.values()))
