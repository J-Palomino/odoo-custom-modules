# -*- coding: utf-8 -*-
"""Tests for spin tickets — the gate on who may play.

The ticket is what makes the wheel spend-one-get-one rather than a
free-for-all, so the guarantees that matter are: you cannot spin without one,
you cannot spend the same one twice, and a failed spin gives it back.
"""
import psycopg2

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase
from odoo.tools import mute_logger


class TestSpinTicket(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Ticket = self.env['mint.spin.ticket']
        self.Pool = self.env['mint.spin.prize']
        self.partner = self.env['res.partner'].create({'name': 'Ticket Tester'})
        self.other = self.env['res.partner'].create({'name': 'Other Tester'})
        self.store = self.env['res.company'].search([('is_dispensary', '=', True)], limit=1)
        self.today = '2026-08-24'
        self.Pool.search([]).unlink()
        self.Ticket.search([]).unlink()

    # ── granting ───────────────────────────────────────────────────────────

    def test_grant_creates_available_tickets(self):
        self.Ticket.grant(self.partner, count=3)
        self.assertEqual(self.Ticket.available_count(self.partner), 3)

    def test_grant_requires_a_partner_and_a_positive_count(self):
        with self.assertRaises(UserError):
            self.Ticket.grant(None, count=1)
        with self.assertRaises(UserError):
            self.Ticket.grant(self.partner, count=0)

    def test_balance_is_per_customer(self):
        self.Ticket.grant(self.partner, count=2)
        self.assertEqual(self.Ticket.available_count(self.other), 0)

    def test_source_is_recorded(self):
        """Where a ticket came from decides what the promotion legally is, so
        it must be visible in the data rather than implied."""
        tickets = self.Ticket.grant(self.partner, count=1, source='purchase')
        self.assertEqual(tickets.source, 'purchase')

    # ── spending ───────────────────────────────────────────────────────────

    def test_spend_one_consumes_exactly_one(self):
        self.Ticket.grant(self.partner, count=2)
        spent = self.Ticket.spend_one(self.partner)
        self.assertTrue(spent)
        self.assertEqual(spent.state, 'spent')
        self.assertTrue(spent.spent_at)
        self.assertEqual(self.Ticket.available_count(self.partner), 1)

    def test_spend_one_returns_none_when_broke(self):
        self.assertIsNone(self.Ticket.spend_one(self.partner))

    def test_a_ticket_is_never_handed_out_twice(self):
        """The atomic UPDATE ... RETURNING is the double-spend guard; two
        spends must return two different rows, never the same one."""
        self.Ticket.grant(self.partner, count=3)
        seen = set()
        for _ in range(3):
            t = self.Ticket.spend_one(self.partner)
            self.assertIsNotNone(t)
            self.assertNotIn(t.id, seen, 'the same ticket was spent twice')
            seen.add(t.id)
        self.assertIsNone(self.Ticket.spend_one(self.partner))

    def test_spending_does_not_touch_another_customer(self):
        self.Ticket.grant(self.other, count=1)
        self.assertIsNone(self.Ticket.spend_one(self.partner))
        self.assertEqual(self.Ticket.available_count(self.other), 1)

    def test_expired_tickets_are_not_spendable(self):
        self.Ticket.grant(self.partner, count=1, expires_at='2020-01-01 00:00:00')
        self.assertEqual(self.Ticket.available_count(self.partner), 0)
        self.assertIsNone(self.Ticket.spend_one(self.partner))

    def test_soonest_expiring_is_spent_first(self):
        """Otherwise an open-ended ticket sits in front of a dated one and the
        customer silently loses the dated one."""
        self.Ticket.grant(self.partner, count=1)  # no expiry
        dated = self.Ticket.grant(self.partner, count=1,
                                  expires_at='2099-01-01 00:00:00')
        spent = self.Ticket.spend_one(self.partner)
        self.assertEqual(spent.id, dated.id)

    def test_sweep_marks_elapsed_tickets_expired(self):
        self.Ticket.grant(self.partner, count=2, expires_at='2020-01-01 00:00:00')
        self.assertEqual(self.Ticket.sweep_expired(), 2)
        self.assertEqual(self.Ticket.available_count(self.partner), 0)

    # ── the gate, end to end ───────────────────────────────────────────────

    def test_cannot_spin_without_a_ticket(self):
        self.Pool.seed_pool({5: 5}, 'test')
        with self.assertRaises(UserError):
            self.Pool.claim_next(self.partner, self.today, self.store)

    def test_claiming_spends_the_ticket_and_links_the_prize(self):
        self.Pool.seed_pool({5: 5}, 'test')
        self.Ticket.grant(self.partner, count=1)
        entry, created = self.Pool.claim_next(self.partner, self.today, self.store)
        self.assertTrue(created)
        self.assertEqual(self.Ticket.available_count(self.partner), 0)
        self.assertTrue(entry.ticket_id)
        self.assertEqual(entry.ticket_id.prize_id, entry)

    def test_multiple_tickets_allow_multiple_spins_the_same_day(self):
        """The per-day cap is gone — the ticket is the limit now."""
        self.Pool.seed_pool({5: 5}, 'test')
        self.Ticket.grant(self.partner, count=2)
        first, _ = self.Pool.claim_next(self.partner, self.today, self.store)
        second, _ = self.Pool.claim_next(self.partner, self.today, self.store)
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(self.Ticket.available_count(self.partner), 0)

    def test_one_ticket_can_only_buy_one_prize(self):
        """UNIQUE(ticket_id) is the backstop if application logic is bypassed."""
        self.Pool.seed_pool({5: 3}, 'test')
        self.Ticket.grant(self.partner, count=1)
        entry, _ = self.Pool.claim_next(self.partner, self.today, self.store)
        spare = self.Pool.search([('state', '=', 'available')], limit=1)
        with self.assertRaises(psycopg2.IntegrityError), mute_logger('odoo.sql_db'):
            spare.write({'ticket_id': entry.ticket_id.id})

    def test_empty_pool_gives_the_ticket_back(self):
        """The spend and the draw share a transaction, so a customer never
        pays a ticket for nothing."""
        self.Ticket.grant(self.partner, count=1)
        with self.assertRaises(UserError):
            self.Pool.claim_next(self.partner, self.today, self.store)
        # The UserError rolled the spend back with it.
        self.assertEqual(self.Ticket.available_count(self.partner), 1)
