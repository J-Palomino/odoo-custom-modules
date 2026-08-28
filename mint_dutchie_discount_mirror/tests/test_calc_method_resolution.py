# -*- coding: utf-8 -*-
"""Stage 1: calculation_method_id is resolved and stored, and nothing else moves.

The whole point of Stage 1 is that it records the method that was ALREADY being
derived at read time. If any of these break, it has stopped being a no-op and
has become a downstream contract change.
"""
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestCalcMethodResolution(TransactionCase):

    def _discount(self, **kw):
        vals = {'name': kw.pop('name', 'Test Discount')}
        vals.update(kw)
        return self.env['mint.discount'].create(vals)

    def test_percent_gets_its_method_stored_on_create(self):
        rec = self._discount(discount_type='percent', discount_amount=0.4)
        self.assertEqual(rec.calculation_method_id, 2)

    def test_loyalty_redemption_resolves_to_percent_off(self):
        """Verified against 8 live rows carrying calc 2 with amount 1.0."""
        rec = self._discount(discount_type='loyalty_redemption', discount_amount=1.0)
        self.assertEqual(rec.calculation_method_id, 2)
        self.assertNotEqual(rec.calculation_method_id, 15)

    def test_ambiguous_bogo_is_left_unset(self):
        """A true BOGO's get-item price is unrecoverable — never guess it."""
        rec = self._discount(discount_type='bogo', discount_amount=0.0)
        self.assertFalse(rec.calculation_method_id)

    def test_bogo_n_for_x_resolves_to_the_total_method(self):
        rec = self._discount(discount_type='bogo', threshold_min=2, discount_amount=80.0)
        self.assertEqual(rec.calculation_method_id, 6)

    def test_resolution_does_not_relabel_discount_type(self):
        """THE Stage 1 invariant. Storing the method must not rewrite the label:
        a 'bogo' row that resolves to 6 must NOT become 'price_per_unit', or
        every consumer reading discount_type sees a silent contract change."""
        rec = self._discount(discount_type='bogo', threshold_min=2, discount_amount=80.0)
        self.assertEqual(rec.calculation_method_id, 6)
        self.assertEqual(rec.discount_type, 'bogo')

    def test_an_explicit_calc_id_is_never_overwritten(self):
        """The Dutchie read sync is authoritative."""
        rec = self._discount(discount_type='percent', calculation_method_id=3)
        self.assertEqual(rec.calculation_method_id, 3)

    def test_resolution_fills_in_on_write_too(self):
        rec = self._discount(discount_type='bogo', discount_amount=0.0)
        self.assertFalse(rec.calculation_method_id)
        rec.write({'threshold_min': 2, 'discount_amount': 45.0})
        self.assertEqual(rec.calculation_method_id, 6)
        self.assertEqual(rec.discount_type, 'bogo')
