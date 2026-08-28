# -*- coding: utf-8 -*-
"""Drift-guard for the Odoo-side canonical discount registry.

The "never again" mechanism mirroring the JS test in letsgomint-us
(__tests__/discountCanonical.test.js). Locks the live-verified semantics so
the calc-method map can't silently re-invert the way it did before 2026-06-05
(Odoo map was inverted on ids 1/2/15; discountSync was missing ids 1/5).

Plain TransactionCase but uses no DB — the canonical module is pure-Python, so
these assertions also pass standalone. Runs under `--test-enable` at install.
"""
from odoo.tests.common import TransactionCase
from odoo.addons.mint_api_v2.models import discount_canonical as dc


class TestDiscountCanonical(TransactionCase):

    def test_registry_self_consistency(self):
        """Every calcMethod row builds its derived maps; id<->canonical bijective."""
        for m in dc.REGISTRY['calcMethods']:
            self.assertEqual(dc.CALC_METHOD_BY_ID[m['id']], m['canonical'])
            self.assertEqual(dc.VALUE_FIELD_BY_ID[m['id']], m['valueField'])
            self.assertEqual(dc.ODOO_TYPE_BY_ID[m['id']], m['odooType'])
            self.assertEqual(dc.CALC_METHOD_TO_ID[m['canonical']], m['id'])

    def test_live_verified_semantics_lock(self):
        """The exact values that were WRONG before — fail loudly on re-inversion."""
        # id 2 was inverted to FIXED_AMOUNT_OFF / 'fixed'
        self.assertEqual(dc.CALC_METHOD_BY_ID[2], 'PERCENT_OFF')
        self.assertEqual(dc.ODOO_TYPE_BY_ID[2], 'percent')
        self.assertEqual(dc.VALUE_FIELD_BY_ID[2], 'discount_amount')
        # id 1 was inverted to PERCENT_OFF / 'percent'
        self.assertEqual(dc.CALC_METHOD_BY_ID[1], 'FLAT_AMOUNT_OFF')
        self.assertEqual(dc.ODOO_TYPE_BY_ID[1], 'fixed')
        self.assertEqual(dc.VALUE_FIELD_BY_ID[1], 'discount_value')
        # id 15 was mislabeled PRICE_TO_AMOUNT_TOTAL / 'bundle'
        self.assertEqual(dc.CALC_METHOD_BY_ID[15], 'LOYALTY')
        self.assertEqual(dc.ODOO_TYPE_BY_ID[15], 'points_multiplier')
        self.assertEqual(dc.VALUE_FIELD_BY_ID[15], 'discount_value')
        # ids 1 & 5 must exist (the discountSync gap that broke $-off deals)
        self.assertEqual(dc.CALC_METHOD_BY_ID[5], 'DOLLAR_OFF_TOTAL')

    def test_odoo_types_are_existing_selection_keys(self):
        """Every odooType reuses an EXISTING mint.discount selection value, so
        the de-mislabel never needs a selection_add."""
        valid = dict(self.env['mint.discount']._fields['discount_type'].selection)
        for m in dc.REGISTRY['calcMethods']:
            self.assertIn(m['odooType'], valid,
                          "registry odooType %r is not a mint.discount.discount_type "
                          "selection key" % m['odooType'])

    def test_parse_raw_restriction(self):
        self.assertEqual(dc.parse_raw_restriction('include: 62375 | 62376'),
                         {'ids': [62375, 62376], 'isExclusion': False})
        self.assertEqual(dc.parse_raw_restriction('exclude: 54'),
                         {'ids': [54], 'isExclusion': True})
        self.assertIsNone(dc.parse_raw_restriction(''))
        self.assertIsNone(dc.parse_raw_restriction(False))

    def test_value_field_resolution(self):
        """discount_value_for reads the RIGHT field per calc method."""
        class _Rec:
            def __init__(self, cmid, amt, val):
                self.calculation_method_id = cmid
                self.discount_type = dc.ODOO_TYPE_BY_ID.get(cmid)
                self.discount_amount = amt
                self.discount_value = val
        # id 1 $5-off: value in discount_value, discount_amount is 0
        self.assertEqual(dc.discount_value_for(_Rec(1, 0.0, 5.0)), 5.0)
        # id 2 percent: value in discount_amount
        self.assertEqual(dc.discount_value_for(_Rec(2, 0.35, 0.0)), 0.35)
        # id 15 loyalty multiplier: value in discount_value
        self.assertEqual(dc.discount_value_for(_Rec(15, 0.0, 4.2)), 4.2)

    # ── Stage 1: calculation_method_id resolution ────────────────────────

    class _Row:
        """Stand-in for a mint.discount record (the resolver is duck-typed)."""
        def __init__(self, **kw):
            self.id = kw.get('id', 1)
            self.calculation_method_id = kw.get('cm', 0)
            self.discount_type = kw.get('dt')
            self.discount_amount = kw.get('amt', 0.0)
            self.discount_value = kw.get('val', 0.0)
            self.threshold_min = kw.get('thr', 0)

    def test_resolve_prefers_the_stored_id(self):
        """The Dutchie read sync is authoritative — never second-guess it."""
        row = self._Row(cm=3, dt='percent')
        self.assertEqual(dc.resolve_calc_method_id(row), 3)

    def test_resolve_falls_back_to_registry_odoo_type(self):
        self.assertEqual(dc.resolve_calc_method_id(self._Row(dt='percent')), 2)
        self.assertEqual(dc.resolve_calc_method_id(self._Row(dt='price_to_amount')), 3)

    def test_loyalty_redemption_is_percent_off_not_the_multiplier(self):
        """A redemption is '100% off this item' (id 2). Id 15 is the loyalty
        MULTIPLIER ('4.2X Loyalty'), a different thing entirely."""
        self.assertEqual(dc.resolve_calc_method_id(self._Row(dt='loyalty_redemption')), 2)
        self.assertNotEqual(dc.resolve_calc_method_id(self._Row(dt='loyalty_redemption')), 15)

    def test_bogo_n_for_x_infers_the_total_method(self):
        """'2 for $80' is Dutchie's PRICE_TO_AMOUNT_TOTAL: threshold + total."""
        row = self._Row(dt='bogo', thr=2, amt=80.0)
        self.assertEqual(dc.resolve_calc_method_id(row), 6)

    def test_true_bogo_stays_unresolved(self):
        """Without a threshold AND a value there is no honest answer — the
        get-item price is not recoverable, so return None rather than guess."""
        self.assertIsNone(dc.resolve_calc_method_id(self._Row(dt='bogo', amt=50.0)))
        self.assertIsNone(dc.resolve_calc_method_id(self._Row(dt='bogo', thr=2)))

    def test_unknown_type_resolves_to_nothing(self):
        self.assertIsNone(dc.resolve_calc_method_id(self._Row(dt='clearance')))
        self.assertIsNone(dc.resolve_calc_method_id(self._Row(dt=None)))

    def test_odoo_only_map_invents_no_dutchie_ids(self):
        """Every alias must point at a method Dutchie actually exposes."""
        for odoo_type, cmid in dc.ODOO_ONLY_TYPE_TO_ID.items():
            self.assertIn(cmid, dc.CALC_METHOD_BY_ID,
                          "%s maps to %s, which is not a Dutchie method" % (odoo_type, cmid))

    def test_ambiguous_bogo_still_safe_defaults_when_stringified(self):
        """Stage 1 stores knowledge; it must not change what is emitted today."""
        row = self._Row(dt='bogo', amt=0.0)
        self.assertEqual(dc.calc_method_string_for(row, warn=False), 'PERCENT_OFF')

