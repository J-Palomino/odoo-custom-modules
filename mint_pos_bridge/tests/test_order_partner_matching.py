# -*- coding: utf-8 -*-
"""_find_or_upgrade_partner must refuse ambiguous identifiers.

Regression cover for the false-account-linking audit. Two defects here bound an
incoming POS order — and its loyalty points — to the wrong customer:

  * phone matching had no 10-digit floor, so a short value in the order payload
    turned Odoo's substring 'ilike' into a wildcard (measured on production:
    'ilike 5551' matched 203 partners, '0000' matched 687); and
  * a bare limit=1 picked an arbitrary row on any collision.

Note the deliberate difference from the consumer web path: this matcher STILL
matches staff. An in-store purchase by an employee really is theirs and should
attach to their partner; the downstream is_staff guard stops Dutchie customer
identity being stamped onto that record. test_staff_partner_is_still_matched
locks that in so a well-meaning "consistency" change cannot quietly remove it.
"""
from unittest.mock import patch

from odoo.tests.common import TransactionCase
from odoo.addons.mint_pos_bridge.controllers import order_api as order_mod


class _FakeRequest:
    def __init__(self, env):
        self.env = env


class TestOrderPartnerMatching(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Partner = self.env['res.partner']
        self.customer = self.Partner.create({
            'name': 'ZZ POS Customer',
            'email': 'zz-pos-uniq@example.test',
            'phone': '5556660001',
            'x_dutchie_customer_id': 'TESTPOS-1',
        })

    def _resolve(self, **customer_data):
        with patch.object(order_mod, 'request', _FakeRequest(self.env)):
            return order_mod._find_or_upgrade_partner(customer_data)

    # --- dutchie id: exact + DB-unique, must keep working ----------------

    def test_dutchie_customer_id_matches(self):
        self.assertEqual(
            self._resolve(dutchie_customer_id='TESTPOS-1'), self.customer)

    # --- phone floor -----------------------------------------------------

    def test_short_phone_does_not_wildcard_match(self):
        # '6660' is a substring of the customer's 5556660001. Unguarded this
        # bound the order to whichever row sorted first.
        self.assertNotEqual(self._resolve(phone='6660'), self.customer)

    def test_full_phone_matches(self):
        self.assertEqual(self._resolve(phone='5556660001'), self.customer)

    def test_formatted_phone_is_normalised_then_matched(self):
        self.assertEqual(self._resolve(phone='(555) 666-0001'), self.customer)

    # --- ambiguity -------------------------------------------------------

    def test_shared_phone_is_refused(self):
        self.Partner.create({
            'name': 'ZZ POS Same Number',
            'email': 'zz-pos-other-uniq@example.test',
            'phone': '5556660001',
        })
        self.assertNotEqual(self._resolve(phone='5556660001'), self.customer)

    def test_shared_email_is_refused(self):
        self.Partner.create({
            'name': 'ZZ POS Duplicate Email',
            'email': 'zz-pos-uniq@example.test',
            'phone': '5557770002',
        })
        self.assertNotEqual(
            self._resolve(email='zz-pos-uniq@example.test'), self.customer)

    def test_ambiguous_phone_falls_through_to_unique_email(self):
        self.Partner.create({
            'name': 'ZZ POS Same Number 2',
            'email': 'zz-pos-other2-uniq@example.test',
            'phone': '5556660001',
        })
        self.assertEqual(
            self._resolve(phone='5556660001', email='zz-pos-uniq@example.test'),
            self.customer,
        )

    # --- deliberate divergence from the web path -------------------------

    def test_staff_partner_is_still_matched(self):
        """In-store purchases by staff attach to their own partner — by design.

        This is intentionally the OPPOSITE of checkout._find_partner, which
        refuses staff. Do not "unify" these without re-reading the is_staff
        comment in _find_or_upgrade_partner: stamping is what's guarded there,
        not matching.
        """
        staff = self.Partner.create({
            'name': 'ZZ POS Staff Shopper',
            'email': 'zz-pos-staff-uniq@example.test',
            'phone': '5558880003',
        })
        self.env['res.users'].create({
            'name': 'ZZ POS Staff Shopper',
            'login': 'zz-pos-staff-uniq-login@example.test',
            'partner_id': staff.id,
        })
        self.assertEqual(self._resolve(phone='5558880003'), staff)

    def test_staff_partner_is_not_stamped_with_dutchie_identity(self):
        """Matching staff is allowed; branding them a Dutchie customer is not."""
        staff = self.Partner.create({
            'name': 'ZZ POS Staff No Stamp',
            'email': 'zz-pos-nostamp-uniq@example.test',
            'phone': '5559990004',
        })
        self.env['res.users'].create({
            'name': 'ZZ POS Staff No Stamp',
            'login': 'zz-pos-nostamp-uniq-login@example.test',
            'partner_id': staff.id,
        })
        self._resolve(phone='5559990004', dutchie_customer_id='SHOULD-NOT-STICK')
        self.assertFalse(staff.x_dutchie_customer_id)
