# -*- coding: utf-8 -*-
"""Signup adopts an existing Dutchie-linked customer instead of orphaning it.

Web signup used to always create a fresh res.partner, so a customer who already
had a Dutchie-backfilled partner (holding their loyalty/history) got a brand-new
orphan with no x_dutchie_customer_id -> 0 points. _find_linkable_dutchie_partner
adopts that existing partner, while never claiming an employee's staff partner.

The match is STRICTER than checkout's (which takes phone OR email): both the
email and the phone must resolve to the SAME single unclaimed Dutchie customer.
Checkout matches per-order, but signup binds a login — a wrong match would hand
over that customer's history permanently. See MR-1089.
"""
from unittest.mock import patch

from odoo.tests.common import TransactionCase
from odoo.addons.mint_customer_api.controllers import auth as auth_mod


class _FakeRequest:
    """Minimal stand-in for odoo.http.request — the helper only needs .env."""
    def __init__(self, env):
        self.env = env


class TestSignupDutchieLink(TransactionCase):

    def setUp(self):
        super().setUp()
        self.ctrl = auth_mod.MintCustomerAuth()
        Partner = self.env['res.partner']
        # A Dutchie customer with no user — the record a web signup should adopt.
        self.dutchie_partner = Partner.create({
            'name': 'ZZ Link Test Customer',
            'email': 'zz-linktest-uniq@example.test',
            'phone': '5559998888',
            'x_dutchie_customer_id': 'TESTZZ-1',
        })

    def _resolve(self, email, phone):
        with patch.object(auth_mod, 'request', _FakeRequest(self.env)):
            return self.ctrl._find_linkable_dutchie_partner(email, phone)

    def test_match_requires_both_email_and_phone(self):
        # Both identifiers agree on one unclaimed Dutchie customer -> adopt.
        # Email match is case-insensitive; phone is normalised to last-10.
        self.assertEqual(
            self._resolve('ZZ-LinkTest-Uniq@EXAMPLE.test', '(555) 999-8888'),
            self.dutchie_partner,
        )

    def test_phone_alone_does_not_adopt(self):
        # Correct phone but a different email must NOT adopt: one identifier is
        # not enough to hand over a customer's history on a login bind.
        self.assertFalse(
            self._resolve('someone-else@example.test', '(555) 999-8888'))

    def test_email_alone_does_not_adopt(self):
        # Correct email, no phone supplied -> refused.
        self.assertFalse(
            self._resolve('zz-linktest-uniq@example.test', ''))

    def test_conflicting_identifiers_do_not_adopt(self):
        # Email resolves to one Dutchie customer, phone to a different one:
        # strong evidence the signup is neither. Refuse rather than guess.
        self.env['res.partner'].create({
            'name': 'ZZ Other Customer',
            'email': 'zz-other-uniq@example.test',
            'phone': '5554443333',
            'x_dutchie_customer_id': 'TESTZZ-3',
        })
        self.assertFalse(
            self._resolve('zz-linktest-uniq@example.test', '5554443333'))

    def test_ambiguous_identifier_does_not_adopt(self):
        # A second unclaimed Dutchie customer sharing the phone makes that
        # identifier ambiguous -> refuse.
        self.env['res.partner'].create({
            'name': 'ZZ Duplicate Phone',
            'email': 'zz-dupphone-uniq@example.test',
            'phone': '5559998888',
            'x_dutchie_customer_id': 'TESTZZ-4',
        })
        self.assertFalse(
            self._resolve('zz-linktest-uniq@example.test', '5559998888'))

    def test_no_dutchie_id_is_not_adopted(self):
        # A plain partner matching on both identifiers but without a Dutchie
        # link is ignored.
        self.env['res.partner'].create({
            'name': 'ZZ Plain', 'email': 'zz-plain-uniq@example.test',
            'phone': '5552221111',
        })
        self.assertFalse(
            self._resolve('zz-plain-uniq@example.test', '5552221111'))

    def test_partner_with_user_is_excluded(self):
        # An employee/staff (or already-web) partner that backs a login must
        # never be adopted, even if it is a Dutchie customer.
        emp = self.env['res.partner'].create({
            'name': 'ZZ Employee', 'email': 'zz-emp-uniq@example.test',
            'phone': '5557776666', 'x_dutchie_customer_id': 'TESTZZ-2',
        })
        self.env['res.users'].create({
            'name': 'ZZ Employee', 'login': 'zz-emp-uniq-login@example.test',
            'partner_id': emp.id,
        })
        self.assertFalse(self._resolve('zz-emp-uniq@example.test', '5557776666'))

    def test_no_match_returns_none(self):
        self.assertFalse(self._resolve('nobody-zz-uniq@example.test', '5550001111'))
