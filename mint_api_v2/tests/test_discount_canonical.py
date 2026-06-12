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
