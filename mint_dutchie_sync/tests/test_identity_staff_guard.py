# -*- coding: utf-8 -*-
"""Keeping staff records out of a customer's identity union.

The union folds a customer's other partner rows into their own view — orders,
loyalty balance, and gift-card money. The one thing it must never fold in is a
STAFF record, and the check that enforced that was `sib.employee or <internal
user>`.

Measured on prod 2026-09-09, `employee` does not carry that meaning: 8,505
partners are named "(EMP)" and every one of them has employee=False, while only
154 rows in 1.8M set the flag at all against 214 hr.employee records. 535 of the
"(EMP)" rows hold a strong dl: key, so they were unionable, and only 58 were
caught by the internal-user clause — roughly 477 staff rows guarded by nothing.

These pin each signal separately so a future data change that kills one is a red
test rather than a silent hole.
"""
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestStaffPartnerDetection(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Partner = cls.env['res.partner']

    def _p(self, name, **vals):
        return self.Partner.create(dict({'name': name}, **vals))

    def test_a_plain_customer_is_not_staff(self):
        p = self._p('Ordinary Customer')
        self.assertEqual(self.Partner._staff_partner_ids([p.id]), set())

    def test_the_employee_flag_still_counts(self):
        p = self._p('Flagged Staff', employee=True)
        self.assertIn(p.id, self.Partner._staff_partner_ids([p.id]))

    def test_the_name_marker_counts(self):
        # The signal this data actually carries. Nothing else catches these.
        p = self._p('James Connery (EMP)')
        self.assertIn(
            p.id, self.Partner._staff_partner_ids([p.id]),
            "an (EMP)-named row must be treated as staff even with "
            "employee=False, which is how all 8,505 of them are stored",
        )

    def test_the_marker_is_case_insensitive_and_positional(self):
        for name in ('jane doe (emp)', '(EMP) Jane Doe', 'Jane (Emp) Doe'):
            p = self._p(name)
            self.assertIn(p.id, self.Partner._staff_partner_ids([p.id]), name)

    def test_the_marker_list_is_configurable(self):
        self.env['ir.config_parameter'].sudo().set_param(
            self.Partner.STAFF_NAME_MARKERS_PARAM, '(EMP),(STAFF)')
        p = self._p('Someone (STAFF)')
        self.assertIn(p.id, self.Partner._staff_partner_ids([p.id]))

    def test_empty_input_is_empty_output(self):
        self.assertEqual(self.Partner._staff_partner_ids([]), set())
        self.assertEqual(self.Partner._staff_partner_ids(None), set())

    def test_a_staff_sibling_is_kept_out_of_the_union(self):
        # End to end: same identity key, one customer row and one (EMP) row.
        key = 'dl:STAFFGUARDTEST1'
        me = self._p('Test Person', x_dutchie_identity_key=key,
                     email='guard.test@example.com')
        staff = self._p('Test Person (EMP)', x_dutchie_identity_key=key)
        self.env['ir.config_parameter'].sudo().set_param(
            me.IDENTITY_UNION_PARAM, '1')
        union = me.identity_union_ids()
        self.assertIn(me.id, union)
        self.assertNotIn(
            staff.id, union,
            "the (EMP) sibling must not be folded into a customer's view",
        )
