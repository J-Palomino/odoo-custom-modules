# -*- coding: utf-8 -*-
"""`is_staff` on the auth payload — the storefront's staff/customer split.

The property these tests pin is that the verdict is DELEGATED, never
reimplemented: whatever `_staff_partner_ids()` says, the auth payload must
agree, or the storefront's analytics filter drifts from Odoo's own staff guard.
"""
from odoo.tests import TransactionCase, tagged

from odoo.addons.mint_customer_api.controllers.auth import _is_staff_partner


class _FakeUser:
    """Stands in for res.users — `_is_staff_partner` only reads `.share`."""

    def __init__(self, share=True):
        self.share = share


@tagged('post_install', '-at_install')
class TestAuthIsStaff(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Partner = self.env['res.partner']

    def _require_resolver(self):
        if not hasattr(self.Partner, '_staff_partner_ids'):
            self.skipTest('mint_dutchie_sync not installed - resolver absent')

    def test_no_partner_falls_back_to_login_signal(self):
        """A falsy partner must not raise; it degrades to the login signal."""
        self.assertFalse(_is_staff_partner(_FakeUser(share=True), None))
        self.assertTrue(_is_staff_partner(_FakeUser(share=False), None))
        self.assertFalse(
            _is_staff_partner(_FakeUser(share=True), self.Partner.browse()))

    def test_plain_customer_is_not_staff(self):
        self._require_resolver()
        p = self.Partner.create({'name': 'Jane Shopper'})
        self.assertFalse(_is_staff_partner(_FakeUser(share=True), p))

    def test_emp_name_marker_is_staff(self):
        """The marker is where the truth lives: 8,505 rows, none set `employee`."""
        self._require_resolver()
        p = self.Partner.create({'name': 'Jane Shopper (EMP)'})
        self.assertTrue(_is_staff_partner(_FakeUser(share=True), p))

    def test_employee_flag_is_staff(self):
        self._require_resolver()
        p = self.Partner.create({'name': 'Flagged Person', 'employee': True})
        self.assertTrue(_is_staff_partner(_FakeUser(share=True), p))

    def test_agrees_with_the_resolver(self):
        """The whole contract: never diverge from Odoo's own staff guard."""
        self._require_resolver()
        for name, employee in [('Plain Customer', False),
                               ('Marked Person (EMP)', False),
                               ('Flagged Person', True)]:
            p = self.Partner.create({'name': name, 'employee': employee})
            self.assertEqual(
                _is_staff_partner(_FakeUser(share=True), p),
                p.id in self.Partner._staff_partner_ids([p.id]),
                'auth verdict diverged from _staff_partner_ids for %s' % name,
            )
