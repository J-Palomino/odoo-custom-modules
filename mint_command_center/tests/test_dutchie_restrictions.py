"""Regression: Dutchie discount restriction assembly.

The rule under test (Odoo #107245): an explicit product include is the
reviewer's pick list and wins outright — the deal publishes Product-only and
Brand/Category INCLUDES are omitted. A brand EXCLUDE survives (it can only
narrow the picks), and product EXCLUDES still take the slot when there is no
include.

This reverses the earlier rule, which dropped the Product include whenever a
Brand/Category was present. That rule came from deal sub 395 ("IO Extracts —
2 for $35"), which published Brand+Category+239 products and applied to only 48
— read at the time as "the Product include over-constrains". That diagnosis was
wrong. Verified against live data (#107245): all 239 picks sat inside the
picker's own scope, all 239 had a valid dutchie_product_id, and all 239 were a
single brand + single Odoo category. So the include was right and the collapse
to 48 means the published Dutchie Category id did not correspond to the Odoo
category — a mapping defect. Dropping the include masked it and shipped a
brand-wide discount overlapping the picks by ~20%. Product-only publishes no
category id, so it cannot be poisoned by that mapping.

Dutchie natively supports Product-only (live records 371881, 380941, 53849).

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

    # ── sub 395 shape: the picks win ─────────────────────────────────────
    def test_brand_category_product_publishes_product_only(self):
        """sub 395 shape: brand+category+products -> Product-only, no Brand/Category."""
        restr, warns = build_dutchie_restrictions(B, [], PI, [], C)
        self.assertEqual(_slot(restr, 'Product'), (False, PI),
                         "the reviewer's picks must reach Dutchie verbatim")
        self.assertEqual(_slot(restr, 'Brand'), (False, []),
                         "Brand include must be omitted so intersection cannot drop a pick")
        self.assertEqual(_slot(restr, 'Category'), (False, []),
                         "Category include must be omitted — it is the #395 mapping defect")
        self.assertTrue(any('Brand/Category' in w for w in warns),
                        "omitting Brand/Category must be surfaced, not silent")

    def test_brand_category_product_include_beats_excludes(self):
        """brand+category+inc+exc -> include takes the slot; exclude warned."""
        restr, warns = build_dutchie_restrictions(B, [], PI, PE, C)
        self.assertEqual(_slot(restr, 'Product'), (False, PI))
        self.assertTrue(any('exclusions dropped' in w for w in warns))

    def test_brand_only_keeps_product_include(self):
        restr, warns = build_dutchie_restrictions(B, [], PI, [], [])
        self.assertEqual(_slot(restr, 'Product'), (False, PI))
        self.assertEqual(_slot(restr, 'Brand'), (False, []))

    def test_category_only_keeps_product_include(self):
        restr, warns = build_dutchie_restrictions([], [], PI, [], C)
        self.assertEqual(_slot(restr, 'Product'), (False, PI))
        self.assertEqual(_slot(restr, 'Category'), (False, []))

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
