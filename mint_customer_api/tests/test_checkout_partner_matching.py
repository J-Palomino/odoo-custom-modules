# -*- coding: utf-8 -*-
"""checkout._find_partner must refuse rather than guess.

Regression cover for the false-account-linking audit. Three defects lived here,
all of which silently bound an order — and the loyalty points on it — to the
wrong customer:

  * a bare limit=1 picked an arbitrary row when an identifier resolved to more
    than one partner (shared phone numbers are common: a 40k-partner sample
    held 1,232 numbers used by more than one partner, worst case 23);
  * no staff guard, so a consumer order could land on an employee identity,
    contradicting the posture auth.py::_create_web_user documents; and
  * phone matching without a 10-digit floor, which turned Odoo's substring
    'ilike' into a wildcard.

Every test below asserts a REFUSAL. Refusing costs a duplicate partner (the
roster sync dedups); guessing costs money and history on the wrong customer.
"""
from unittest.mock import patch

from odoo.tests.common import TransactionCase
from odoo.addons.mint_customer_api.controllers import checkout as checkout_mod


class _FakeRequest:
    """Minimal stand-in for odoo.http.request — the helper only needs .env."""

    def __init__(self, env):
        self.env = env


class TestCheckoutPartnerMatching(TransactionCase):

    def setUp(self):
        super().setUp()
        self.ctrl = checkout_mod.MintCheckout()
        self.Partner = self.env['res.partner']
        self.customer = self.Partner.create({
            'name': 'ZZ Checkout Customer',
            'email': 'zz-checkout-uniq@example.test',
            'phone': '5551110001',
        })

    def _find(self, phone, email):
        with patch.object(checkout_mod, 'request', _FakeRequest(self.env)):
            return self.ctrl._find_partner(phone, email)

    # --- happy paths -----------------------------------------------------

    def test_unique_phone_matches(self):
        self.assertEqual(self._find('5551110001', ''), self.customer)

    def test_unique_email_matches_case_insensitively(self):
        self.assertEqual(
            self._find('', 'ZZ-Checkout-Uniq@EXAMPLE.test'), self.customer)

    # --- ambiguity -------------------------------------------------------

    def test_shared_phone_is_refused_not_guessed(self):
        # Two partners on one number — the real-world case. The old limit=1
        # bound the order to whichever row sorted first.
        self.Partner.create({
            'name': 'ZZ Same Number Household',
            'email': 'zz-household-uniq@example.test',
            'phone': '5551110001',
        })
        self.assertFalse(self._find('5551110001', ''))

    def test_shared_email_is_refused(self):
        self.Partner.create({
            'name': 'ZZ Duplicate Email',
            'email': 'zz-checkout-uniq@example.test',
            'phone': '5559990000',
        })
        self.assertFalse(self._find('', 'zz-checkout-uniq@example.test'))

    def test_ambiguous_phone_falls_through_to_a_unique_email(self):
        # An ambiguous identifier must not end the search on a bad match: the
        # next identifier still gets its chance.
        self.Partner.create({
            'name': 'ZZ Same Number Other',
            'email': 'zz-other-uniq@example.test',
            'phone': '5551110001',
        })
        self.assertEqual(
            self._find('5551110001', 'zz-checkout-uniq@example.test'),
            self.customer,
        )

    # --- staff -----------------------------------------------------------

    def test_partner_backing_an_internal_user_is_refused(self):
        staff = self.Partner.create({
            'name': 'ZZ Staff Member',
            'email': 'zz-staff-uniq@example.test',
            'phone': '5552220002',
        })
        self.env['res.users'].create({
            'name': 'ZZ Staff Member',
            'login': 'zz-staff-uniq-login@example.test',
            'partner_id': staff.id,
            # share=False (internal) is the default for a created user
        })
        self.assertFalse(self._find('5552220002', 'zz-staff-uniq@example.test'))

    def test_employee_flagged_partner_is_refused(self):
        self.Partner.create({
            'name': 'ZZ Employee Contact',
            'email': 'zz-emp-contact-uniq@example.test',
            'phone': '5553330003',
            'employee': True,
        })
        self.assertFalse(
            self._find('5553330003', 'zz-emp-contact-uniq@example.test'))

    def test_portal_customer_is_still_matched(self):
        # The staff guard keys on share=False. A portal (share=True) customer
        # is a real web customer and must keep matching.
        portal = self.Partner.create({
            'name': 'ZZ Portal Customer',
            'email': 'zz-portal-uniq@example.test',
            'phone': '5554440004',
        })
        group_portal = self.env.ref('base.group_portal')
        self.env['res.users'].create({
            'name': 'ZZ Portal Customer',
            'login': 'zz-portal-uniq-login@example.test',
            'partner_id': portal.id,
            'group_ids': [(6, 0, [group_portal.id])],
        })
        self.assertEqual(
            self._find('5554440004', 'zz-portal-uniq@example.test'), portal)

    # --- short phone / substring wildcard --------------------------------

    def test_short_phone_never_substring_matches(self):
        # _normalize_phone returns a short string unchanged, and 'ilike' is a
        # substring match — unguarded, '1110' would wildcard across the table.
        self.assertFalse(self._find('1110', ''))

    def test_short_phone_falls_through_to_email(self):
        self.assertEqual(
            self._find('1110', 'zz-checkout-uniq@example.test'), self.customer)

    def test_no_identifiers_returns_nothing(self):
        self.assertFalse(self._find('', ''))
