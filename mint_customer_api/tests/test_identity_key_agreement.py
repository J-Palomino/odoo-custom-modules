# -*- coding: utf-8 -*-
"""The signup key and the roster key must describe the same human identically.

x_dutchie_identity_key is how a web signup finds the POS record for the same
person. Two paths build it — the roster import in mint_dutchie_sync and the
signup here — and they were held in agreement by a comment saying "MUST stay
byte-identical". Nothing tested it.

That is the dangerous shape: a drifted normalisation does not raise. It silently
matches nothing, and the 1.79M keys already stored become unreachable for
whichever path changed. Someone would notice weeks later as "linking stopped
working", exactly as #119776 was reported.

mint_dutchie_sync.identity is now the single definition of the format, and this
pins the signup path to it. The two implementations stay separate on purpose:
mint_customer_api deliberately does NOT depend on mint_dutchie_sync — see
_create_web_user, "adding it would couple the consumer signup API to the roster
importer" — and probes _fields rather than assume the roster module is present.
So the guarantee has to be a test rather than an import.
"""
import datetime

from odoo.tests.common import TransactionCase
from odoo.addons.mint_customer_api.controllers import auth as auth_mod

try:
    from odoo.addons.mint_dutchie_sync.identity import identity_key as canonical
    from odoo.addons.mint_dutchie_sync.models.dutchie_sync_checkpoint import (
        ROSTER_COLS, DutchieSyncCheckpoint,
    )
    _HAS_SYNC = True
except ImportError:  # pragma: no cover - roster module not installed
    _HAS_SYNC = False


class TestIdentityKeyAgreement(TransactionCase):
    """Signup output == canonical output, for the same person."""

    def setUp(self):
        super().setUp()
        if not _HAS_SYNC:
            self.skipTest('mint_dutchie_sync not installed')
        self.signup = auth_mod.MintCustomerAuth._dutchie_identity_key

    # -- the licence path ------------------------------------------------

    def test_licence_matches_canonical(self):
        key = self.signup({'documentNumber': 'd0123456'}, 'Jane Doe',
                          datetime.date(1990, 3, 4))
        self.assertEqual(key, canonical(dl='d0123456'))
        self.assertEqual(key, 'dl:D0123456')

    def test_licence_is_trimmed_and_uppercased(self):
        self.assertEqual(self.signup({'documentNumber': '  d55 '}, None, None),
                         canonical(dl='  d55 '))

    def test_snake_case_field_is_accepted(self):
        # The FE has posted both spellings; they must key identically.
        self.assertEqual(self.signup({'document_number': 'D9'}, None, None),
                         self.signup({'documentNumber': 'D9'}, None, None))

    # -- the name+DOB path -----------------------------------------------

    def test_name_dob_matches_canonical_in_roster_date_format(self):
        # The roster stores MM/DD/YYYY. This is the trap: the ID scanner posts
        # ISO, and an ISO key matches no stored row.
        key = self.signup({}, 'jane doe', datetime.date(1990, 3, 4))
        self.assertEqual(key, canonical(name='jane doe', dob='03/04/1990'))
        self.assertEqual(key, 'nd:JANE DOE|03/04/1990')
        self.assertNotIn('1990-03-04', key or '')

    def test_licence_wins_over_name_dob(self):
        # Precedence must match the order the stored keys were generated in.
        self.assertEqual(
            self.signup({'documentNumber': 'D1'}, 'Jane Doe', datetime.date(1990, 3, 4)),
            'dl:D1')

    # -- nothing usable --------------------------------------------------

    def test_no_identifier_is_none_not_a_guess(self):
        # A non-deterministic key is worse than none: it creates a partner that
        # can never be matched again.
        self.assertIsNone(self.signup({}, None, None))
        self.assertIsNone(self.signup({}, 'Jane Doe', None))
        self.assertIsNone(self.signup({'documentNumber': '   '}, None, None))

    # -- the roster side, through the real column mapping ----------------

    def test_roster_row_matches_canonical(self):
        row = {ROSTER_COLS['dl']: 'd0123456'}
        self.assertEqual(DutchieSyncCheckpoint._identity_key(row), 'dl:D0123456')

    def test_roster_precedence_dl_over_mj_over_name_over_phone(self):
        base = {
            ROSTER_COLS['dl']: '', ROSTER_COLS['mj_state_id']: '',
            ROSTER_COLS['name']: 'JANE DOE', ROSTER_COLS['dob']: '03/04/1990',
            ROSTER_COLS['phone']: '5550100',
        }
        self.assertEqual(DutchieSyncCheckpoint._identity_key(base),
                         'nd:JANE DOE|03/04/1990')
        self.assertEqual(
            DutchieSyncCheckpoint._identity_key({**base, ROSTER_COLS['mj_state_id']: 'mj9'}),
            'mj:MJ9')
        self.assertEqual(
            DutchieSyncCheckpoint._identity_key({**base, ROSTER_COLS['dl']: 'dl9'}),
            'dl:DL9')
        self.assertIsNone(DutchieSyncCheckpoint._identity_key(
            {ROSTER_COLS['phone']: '', ROSTER_COLS['cellphone']: ''}))

    def test_a_roster_row_and_a_signup_for_one_person_agree(self):
        """The property the whole feature rests on."""
        dl = 'D0123456'
        roster = DutchieSyncCheckpoint._identity_key({ROSTER_COLS['dl']: dl})
        signup = self.signup({'documentNumber': dl}, 'Jane Doe', datetime.date(1990, 3, 4))
        self.assertEqual(roster, signup)

        # ...and with no licence on either side.
        roster_nd = DutchieSyncCheckpoint._identity_key({
            ROSTER_COLS['name']: 'JANE DOE', ROSTER_COLS['dob']: '03/04/1990'})
        signup_nd = self.signup({}, 'Jane Doe', datetime.date(1990, 3, 4))
        self.assertEqual(roster_nd, signup_nd)
