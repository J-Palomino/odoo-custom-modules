"""Stage 2 foundation: unified ExternalId + create-vs-update resolution (#2).

These are the building blocks for converging the two Dutchie publish paths onto
one record. dutchie_deal_external_id gives both paths the same deal-keyed id;
resolve_dutchie_discount_id encodes the idempotent upsert decision, with the
hard rule that a FAILED read-back never falls through to a blind create.
"""
from odoo.tests import TransactionCase, tagged

from ..models.deal_mixins import (dutchie_deal_external_id,
                                   resolve_dutchie_discount_id)


@tagged('post_install', '-at_install')
class TestDutchieIdentity(TransactionCase):

    def test_external_id_single_and_multispan(self):
        self.assertEqual(dutchie_deal_external_id(22408, 575), 'lgm_deal_22408_lsp575')
        self.assertEqual(dutchie_deal_external_id(22408, 575, 1), 'lgm_deal_22408_lsp575_w1')
        self.assertEqual(dutchie_deal_external_id(22408, 723, 2), 'lgm_deal_22408_lsp723_w2')

    def test_external_id_per_market_distinct(self):
        # same deal, different markets -> different keys (no collision)
        self.assertNotEqual(dutchie_deal_external_id(1, 575),
                            dutchie_deal_external_id(1, 723))

    def test_resolve_readback_found_is_authoritative(self):
        # Dutchie says id 999 exists -> update it, even if our local id differs.
        self.assertEqual(resolve_dutchie_discount_id(0, 999), (999, 'update'))
        self.assertEqual(resolve_dutchie_discount_id(123, 999), (999, 'update'))

    def test_resolve_confirmed_absent_creates(self):
        self.assertEqual(resolve_dutchie_discount_id(0, 0), (0, 'create'))
        # even if we had a stale local id, Dutchie confirms it's gone -> create
        self.assertEqual(resolve_dutchie_discount_id(123, 0), (0, 'create'))

    def test_resolve_unknown_never_blind_creates(self):
        # read failed (None) + no local id -> abort, do NOT create a dup
        self.assertEqual(resolve_dutchie_discount_id(0, None), (0, 'abort'))
        # read failed + known local id -> best-effort update
        self.assertEqual(resolve_dutchie_discount_id(123, None), (123, 'update'))

    def test_resolve_bool_treated_as_unknown(self):
        # bool is an int subclass; must not be read as a found id
        self.assertEqual(resolve_dutchie_discount_id(0, True), (0, 'abort'))
        self.assertEqual(resolve_dutchie_discount_id(55, False), (55, 'update'))
