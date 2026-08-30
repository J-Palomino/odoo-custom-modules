# -*- coding: utf-8 -*-
"""Tests for granting spin tickets against an external reference.

The purchase importer runs in bulk and has been observed days behind, so the
guarantees that matter here are: a re-imported receipt never pays twice, and
the feature stays off until someone deliberately turns it on.
"""
import psycopg2

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase
from odoo.tools import mute_logger


class TestSpinTicketSourceRef(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Ticket = self.env['mint.spin.ticket']
        self.Config = self.env['mint.config']
        self.partner = self.env['res.partner'].create({'name': 'Buyer'})
        self.ref = 'purchase:69:178643122'
        self.Ticket.search([]).unlink()
        self.Config.search([('key', '=', 'spin.purchase_grant')]).unlink()

    # ── idempotency ────────────────────────────────────────────────────────

    def test_grants_the_requested_number(self):
        made = self.Ticket.grant_for_ref(self.partner, self.ref, count=2, source='purchase')
        self.assertEqual(len(made), 2)
        self.assertEqual(self.Ticket.available_count(self.partner), 2)

    def test_replaying_the_same_ref_grants_nothing(self):
        """A re-imported receipt must not pay the customer twice."""
        self.Ticket.grant_for_ref(self.partner, self.ref, count=1, source='purchase')
        again = self.Ticket.grant_for_ref(self.partner, self.ref, count=1, source='purchase')
        self.assertEqual(len(again), 0)
        self.assertEqual(self.Ticket.available_count(self.partner), 1)

    def test_topping_up_grants_only_the_difference(self):
        self.Ticket.grant_for_ref(self.partner, self.ref, count=1, source='purchase')
        more = self.Ticket.grant_for_ref(self.partner, self.ref, count=3, source='purchase')
        self.assertEqual(len(more), 2)
        self.assertEqual(self.Ticket.available_count(self.partner), 3)

    def test_unique_constraint_is_the_real_guard(self):
        """Application logic skips duplicates; the constraint is what holds if
        something bypasses it."""
        self.Ticket.grant_for_ref(self.partner, self.ref, count=1, source='purchase')
        with self.assertRaises(psycopg2.IntegrityError), mute_logger('odoo.sql_db'):
            self.Ticket.create({
                'partner_id': self.partner.id, 'source': 'purchase',
                'source_ref': self.ref, 'source_seq': 1,
            })

    def test_manual_grants_are_unaffected_by_the_constraint(self):
        """NULL source_ref rows are distinct in Postgres, so staff grants stay
        unlimited."""
        self.Ticket.grant(self.partner, count=5)
        self.assertEqual(self.Ticket.available_count(self.partner), 5)

    def test_different_receipts_grant_separately(self):
        self.Ticket.grant_for_ref(self.partner, self.ref, count=1, source='purchase')
        self.Ticket.grant_for_ref(self.partner, 'purchase:69:999', count=1, source='purchase')
        self.assertEqual(self.Ticket.available_count(self.partner), 2)

    def test_requires_partner_and_ref(self):
        with self.assertRaises(UserError):
            self.Ticket.grant_for_ref(None, self.ref)
        with self.assertRaises(UserError):
            self.Ticket.grant_for_ref(self.partner, '')

    def test_source_is_recorded_as_purchase(self):
        """Purchase-sourced tickets must be findable — they are the ones that
        change what this promotion legally is."""
        made = self.Ticket.grant_for_ref(self.partner, self.ref, count=1, source='purchase')
        self.assertEqual(made.source, 'purchase')

    # ── the config gate ────────────────────────────────────────────────────

    def test_grants_are_disabled_by_default(self):
        settings = self.Ticket.purchase_grant_settings()
        self.assertFalse(settings['enabled'])

    def test_defaults_are_conservative(self):
        settings = self.Ticket.purchase_grant_settings()
        self.assertEqual(settings['tickets'], 1)
        # A narrow window is what stops a catch-up import minting a backlog.
        self.assertLessEqual(settings['max_age_days'], 7)

    def test_config_overrides_defaults(self):
        self.Config.create({
            'key': 'spin.purchase_grant', 'is_active': True,
            'value': '{"enabled": true, "tickets": 2, "min_spend": 25}',
        })
        settings = self.Ticket.purchase_grant_settings()
        self.assertTrue(settings['enabled'])
        self.assertEqual(settings['tickets'], 2)
        self.assertEqual(settings['min_spend'], 25)
        # Unspecified keys keep their defaults rather than vanishing.
        self.assertIn('max_age_days', settings)

    def test_broken_config_json_fails_closed(self):
        """A typo in the config must not switch grants on, or crash the import."""
        self.Config.create({
            'key': 'spin.purchase_grant', 'is_active': True, 'value': '{not json',
        })
        self.assertFalse(self.Ticket.purchase_grant_settings()['enabled'])

    def test_inactive_config_is_ignored(self):
        self.Config.create({
            'key': 'spin.purchase_grant', 'is_active': False,
            'value': '{"enabled": true}',
        })
        self.assertFalse(self.Ticket.purchase_grant_settings()['enabled'])
