# -*- coding: utf-8 -*-
"""Claiming in-store history after the account already exists.

Sign-in with Google produces an account that can never link its POS record:
/api/v1/auth/google is handed only {email, name, google_sub}, so the
link_token never arrives (it dies with the redirect to accounts.google.com)
and _create_web_user is called with no phone, which makes the email+phone
adoption unreachable by construction. claim_history is the way back — scan
again while signed in and the login is repointed onto the roster partner.

What is covered here is the part that must not be got wrong: the token is
the ONLY thing authorising a repoint, so these assert that a token cannot be
replayed against another licence, cannot outlive its window, and cannot be
tampered with; that the bind still refuses a claimed or re-keyed record; and
that a repoint neither strands the customer's own rows nor silently moves
financial ones.

The route itself is not exercised — it needs an HTTP request, a JWT and the
FE key — so each helper it composes is tested directly instead, the same way
test_signup_dutchie_link covers the adoption matcher.
"""
import hashlib
import hmac
import time

from unittest.mock import patch

from odoo.tests.common import TransactionCase
from odoo.addons.mint_customer_api.controllers import auth as auth_mod


class _FakeRequest:
    """Minimal stand-in for odoo.http.request — the helpers only need .env."""
    def __init__(self, env):
        self.env = env


class TestClaimHistory(TransactionCase):

    def setUp(self):
        super().setUp()
        self.ctrl = auth_mod.MintCustomerAuth()
        Partner = self.env['res.partner']
        # The roster record holding the purchase history — unclaimed, which
        # is what makes it linkable.
        self.roster = Partner.create({
            'name': 'ZZ Claim Roster',
            'email': 'zz-claim-roster-uniq@example.test',
            'phone': '5551234567',
            'x_dutchie_customer_id': 'TESTZZ-CLAIM-1',
            'x_dutchie_identity_key': 'dl:ZZTEST1234',
        })
        # The orphan the customer is actually signed into.
        self.orphan = Partner.create({
            'name': 'ZZ Claim Orphan',
            'email': 'zz-claim-orphan-uniq@example.test',
            'is_web_customer': True,
        })

    def _ctx(self):
        return patch.object(auth_mod, 'request', _FakeRequest(self.env))

    def _secret(self):
        return self.env['ir.config_parameter'].sudo().get_param('database.secret')

    def _token(self, partner_id, identity_key, exp):
        """Build a token by hand so expiry can be placed in the past without
        patching the clock out from under Odoo."""
        msg = '%d|%d|%s' % (partner_id, exp, identity_key)
        sig = hmac.new(self._secret().encode(), msg.encode(),
                       hashlib.sha256).hexdigest()
        return '%d.%d.%s' % (partner_id, exp, sig)

    # --- token: the only thing that authorises a repoint -----------------

    def test_token_round_trips_for_its_own_licence(self):
        with self._ctx():
            token = self.ctrl._sign_link_token(self.roster.id, 'dl:ZZTEST1234')
            self.assertEqual(
                self.ctrl._verify_link_token(token, 'dl:ZZTEST1234'),
                self.roster.id,
            )

    def test_token_does_not_verify_for_another_licence(self):
        # The licence is folded into the signature precisely so a token minted
        # from one person's scan cannot be presented for someone else's.
        with self._ctx():
            token = self.ctrl._sign_link_token(self.roster.id, 'dl:ZZTEST1234')
            self.assertIsNone(
                self.ctrl._verify_link_token(token, 'dl:SOMEONEELSE'))

    def test_expired_token_is_rejected(self):
        with self._ctx():
            stale = self._token(self.roster.id, 'dl:ZZTEST1234',
                                int(time.time()) - 1)
            self.assertIsNone(
                self.ctrl._verify_link_token(stale, 'dl:ZZTEST1234'))

    def test_repointed_partner_id_is_rejected(self):
        # Swapping the partner id in an otherwise valid token is the obvious
        # attack — claim someone else's record with your own scan.
        with self._ctx():
            token = self.ctrl._sign_link_token(self.roster.id, 'dl:ZZTEST1234')
            _, exp, sig = token.split('.', 2)
            forged = '%d.%s.%s' % (self.orphan.id, exp, sig)
            self.assertIsNone(
                self.ctrl._verify_link_token(forged, 'dl:ZZTEST1234'))

    def test_malformed_token_is_rejected(self):
        with self._ctx():
            for bad in ('', 'nonsense', '1.2', 'x.y.z', None):
                self.assertIsNone(
                    self.ctrl._verify_link_token(bad, 'dl:ZZTEST1234'), bad)

    # --- the orders guard ------------------------------------------------

    def test_clean_orphan_strands_nothing(self):
        with self._ctx():
            self.assertEqual(self.ctrl._orphan_order_count(self.orphan), 0)

    def test_orphan_with_an_order_is_counted(self):
        # sale.order rather than mint.pos.order on purpose: sale_management is
        # a declared dependency, mint_pos_bridge is not — the probe inside
        # _orphan_order_count exists for exactly that reason.
        self.env['sale.order'].create({'partner_id': self.orphan.id})
        with self._ctx():
            self.assertTrue(self.ctrl._orphan_order_count(self.orphan) >= 1)

    # --- carrying the customer's own rows across ------------------------

    def test_favorites_move_to_the_claimed_partner(self):
        fav = self.env['mint.customer.favorite'].create({
            'partner_id': self.orphan.id,
            'item_type': 'product',
            'item_ref': 'ZZ-CLAIM-PROD-1',
        })
        with self._ctx():
            moved = self.ctrl._move_customer_rows(self.orphan.id, self.roster.id)
        self.assertEqual(fav.partner_id, self.roster)
        self.assertEqual(moved.get('mint.customer.favorite'), 1)

    def test_move_is_a_noop_with_nothing_to_carry(self):
        with self._ctx():
            self.assertEqual(
                self.ctrl._move_customer_rows(self.orphan.id, self.roster.id), {})

    # --- the bind still re-checks everything in-transaction --------------

    def test_bind_refuses_a_record_that_already_has_a_login(self):
        # A claimed record is somebody's account. The token may be minutes old,
        # so this is re-checked at write time rather than trusted from lookup.
        self.env['res.users'].create({
            'name': 'ZZ Claim Existing',
            'login': 'zz-claim-existing-uniq@example.test',
            'partner_id': self.roster.id,
        })
        with self._ctx():
            self.assertIsNone(self.ctrl._bind_link_partner(
                self.roster.id, 'dl:ZZTEST1234',
                'a@example.test', 'ZZ', '', None, None, None))

    def test_bind_refuses_when_the_key_changed_since_lookup(self):
        # A roster re-sync between lookup and claim can rewrite the key; the
        # bind must not land on a record that is no longer the same identity.
        with self._ctx():
            self.assertIsNone(self.ctrl._bind_link_partner(
                self.roster.id, 'dl:DIFFERENTKEY',
                'a@example.test', 'ZZ', '', None, None, None))

    def test_bind_fills_gaps_and_flags_for_review(self):
        # Roster data wins: the name and the contact details already on file
        # are how the store reaches this customer. Only genuine gaps fill.
        gapped = self.env['res.partner'].create({
            'name': 'ZZ Roster No Contact',
            'x_dutchie_customer_id': 'TESTZZ-CLAIM-2',
            'x_dutchie_identity_key': 'dl:ZZTEST5678',
        })
        with self._ctx():
            bound = self.ctrl._bind_link_partner(
                gapped.id, 'dl:ZZTEST5678',
                'zz-fill-uniq@example.test', 'Ignored Name', '5559990000',
                None, None, None)
        self.assertEqual(bound, gapped)
        self.assertEqual(gapped.email, 'zz-fill-uniq@example.test')
        self.assertEqual(gapped.phone, '5559990000')
        # Never overwritten by signup data.
        self.assertEqual(gapped.name, 'ZZ Roster No Contact')
        self.assertTrue(gapped.is_web_customer)
        # The out-of-band control: staff confirm the licence at the register.
        self.assertTrue(gapped.x_merge_needs_review)

    def test_bind_does_not_overwrite_contact_already_on_file(self):
        with self._ctx():
            bound = self.ctrl._bind_link_partner(
                self.roster.id, 'dl:ZZTEST1234',
                'zz-other-uniq@example.test', 'ZZ', '5550000000',
                None, None, None)
        self.assertEqual(bound, self.roster)
        self.assertEqual(self.roster.email, 'zz-claim-roster-uniq@example.test')
        self.assertEqual(self.roster.phone, '5551234567')
