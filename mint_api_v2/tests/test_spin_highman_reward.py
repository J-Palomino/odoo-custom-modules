# -*- coding: utf-8 -*-
"""Tests for the High-man win reward.

Winning the word game earns a spin ticket rather than loyalty points, because
Dutchie's loyalty API is read-only and an Odoo-side point grant would be
invisible at the register. The guarantees that matter here are: one ticket per
customer per puzzle day however many times the win is submitted, and a reward
failure never costs the player anything.
"""
from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase


class TestHighmanReward(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Ticket = self.env['mint.spin.ticket']
        self.Config = self.env['mint.config']
        self.partner = self.env['res.partner'].create({'name': 'Player One'})
        self.other = self.env['res.partner'].create({'name': 'Player Two'})
        self.today = '2026-08-27'
        self.tomorrow = '2026-08-28'
        self.Ticket.search([]).unlink()
        self.Config.search([('key', '=', 'spin.highman_reward')]).unlink()

    # ── the daily cap ──────────────────────────────────────────────────────

    def test_a_win_grants_a_ticket(self):
        granted = self.Ticket.grant_highman_win(self.partner, self.today)
        self.assertEqual(len(granted), 1)
        self.assertEqual(self.Ticket.available_count(self.partner), 1)

    def test_the_ticket_is_promo_sourced_not_purchase(self):
        """Winning a game costs nothing and buys nothing — the source must not
        imply otherwise, since purchase-sourced tickets change what the wheel
        legally is."""
        granted = self.Ticket.grant_highman_win(self.partner, self.today)
        self.assertEqual(granted.source, 'promo')

    def test_a_second_win_the_same_day_grants_nothing(self):
        self.Ticket.grant_highman_win(self.partner, self.today)
        again = self.Ticket.grant_highman_win(self.partner, self.today)
        self.assertEqual(len(again), 0, 'replay must not pay twice')
        self.assertEqual(self.Ticket.available_count(self.partner), 1)

    def test_the_next_day_earns_again(self):
        self.Ticket.grant_highman_win(self.partner, self.today)
        tomorrow = self.Ticket.grant_highman_win(self.partner, self.tomorrow)
        self.assertEqual(len(tomorrow), 1)
        self.assertEqual(self.Ticket.available_count(self.partner), 2)

    def test_players_are_independent(self):
        self.Ticket.grant_highman_win(self.partner, self.today)
        theirs = self.Ticket.grant_highman_win(self.other, self.today)
        self.assertEqual(len(theirs), 1)
        self.assertEqual(self.Ticket.available_count(self.other), 1)

    def test_the_ref_is_keyed_to_the_partner_and_date(self):
        granted = self.Ticket.grant_highman_win(self.partner, self.today)
        self.assertEqual(granted.source_ref,
                         'highman:%s:%s' % (self.today, self.partner.id))

    # ── configuration ──────────────────────────────────────────────────────

    def test_rewards_are_enabled_by_default(self):
        """Unlike purchase grants, this one ships on: a game win carries none
        of the purchase -> chance -> prize weight."""
        self.assertTrue(self.Ticket.highman_reward_settings()['enabled'])

    def test_config_can_disable_rewards(self):
        self.Config.create({
            'key': 'spin.highman_reward', 'is_active': True,
            'value': '{"enabled": false}',
        })
        self.assertEqual(len(self.Ticket.grant_highman_win(self.partner, self.today)), 0)
        self.assertEqual(self.Ticket.available_count(self.partner), 0)

    def test_config_can_change_the_ticket_count(self):
        self.Config.create({
            'key': 'spin.highman_reward', 'is_active': True,
            'value': '{"enabled": true, "tickets": 3}',
        })
        self.assertEqual(len(self.Ticket.grant_highman_win(self.partner, self.today)), 3)

    def test_broken_config_json_falls_back_to_defaults(self):
        self.Config.create({
            'key': 'spin.highman_reward', 'is_active': True, 'value': '{nope',
        })
        settings = self.Ticket.highman_reward_settings()
        self.assertTrue(settings['enabled'])
        self.assertEqual(settings['tickets'], 1)

    # ── input ──────────────────────────────────────────────────────────────

    def test_requires_a_partner_and_a_date(self):
        with self.assertRaises(UserError):
            self.Ticket.grant_highman_win(None, self.today)
        with self.assertRaises(UserError):
            self.Ticket.grant_highman_win(self.partner, '')
