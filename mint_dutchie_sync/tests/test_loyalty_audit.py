# -*- coding: utf-8 -*-
"""Tests for the loyalty audit trail and the automatic-award gate."""
from odoo.tests import TransactionCase, tagged

from ..models.loyalty_card import AWARD_MODE_PARAM, SOURCE_CTX


@tagged('post_install', '-at_install')
class TestLoyaltyAudit(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.partner = cls.env['res.partner'].create({'name': 'Audit Test Customer'})
        cls.program = cls.env['loyalty.program'].search(
            [('program_type', '=', 'loyalty')], limit=1)
        if not cls.program:
            cls.program = cls.env['loyalty.program'].create({
                'name': 'Test Loyalty', 'program_type': 'loyalty',
            })
        cls.company = cls.env.company

    def _history(self, card):
        return self.env['loyalty.history'].search([('card_id', '=', card.id)])

    def _card(self, points=0.0):
        return self.env['loyalty.card'].create({
            'partner_id': self.partner.id,
            'program_id': self.program.id,
            'points': points,
        })

    # -- audit trail ---------------------------------------------------

    def test_write_records_increase(self):
        card = self._card(0.0)
        before = len(self._history(card))
        card.write({'points': 250.0})
        rows = self._history(card)
        self.assertEqual(len(rows) - before, 1, 'one audit row per point change')
        row = rows.sorted('id')[-1]
        self.assertEqual(row.issued, 250.0)
        self.assertEqual(row.used, 0.0)

    def test_write_records_decrease_as_used(self):
        card = self._card(500.0)
        card.write({'points': 200.0})
        row = self._history(card).sorted('id')[-1]
        self.assertEqual(row.used, 300.0, 'a decrease is recorded as "used"')
        self.assertEqual(row.issued, 0.0)

    def test_no_row_when_points_unchanged(self):
        card = self._card(100.0)
        before = len(self._history(card))
        card.write({'points': 100.0})
        self.assertEqual(len(self._history(card)), before,
                         'writing the same value is not a movement')

    def test_no_row_for_unrelated_write(self):
        card = self._card(100.0)
        before = len(self._history(card))
        card.write({'code': 'AUDIT-TEST-CODE'})
        self.assertEqual(len(self._history(card)), before,
                         'only point changes are audited')

    def test_create_with_opening_balance_is_audited(self):
        card = self._card(750.0)
        rows = self._history(card)
        self.assertEqual(len(rows), 1, 'a card born with points is a movement')
        self.assertEqual(rows.issued, 750.0)
        self.assertIn('Opening balance', rows.description)

    def test_create_with_zero_balance_is_not_audited(self):
        card = self._card(0.0)
        self.assertFalse(self._history(card))

    def test_source_label_is_recorded(self):
        card = self._card(0.0)
        card.with_context(**{SOURCE_CTX: 'unit test harness'}).write({'points': 10.0})
        self.assertIn('unit test harness', self._history(card).sorted('id')[-1].description)

    def test_unattributed_write_still_audited(self):
        """The whole point: even a caller that knows nothing about us is logged."""
        card = self._card(0.0)
        card.write({'points': 42.0})
        self.assertIn('unattributed', self._history(card).sorted('id')[-1].description)

    def test_multi_card_write_audits_each(self):
        c1, c2 = self._card(10.0), self._card(20.0)
        before = len(self._history(c1)) + len(self._history(c2))
        (c1 | c2).write({'points': 99.0})
        after = len(self._history(c1)) + len(self._history(c2))
        self.assertEqual(after - before, 2, 'batch writes audit every card')

    def test_wizard_is_not_double_logged(self):
        card = self._card(100.0)
        before = len(self._history(card))
        wizard = self.env['loyalty.card.update.balance'].create({
            'card_id': card.id, 'new_balance': 300.0,
            'description': 'wizard adjustment under test',
        })
        wizard.action_update_card_point()
        self.assertEqual(card.points, 300.0)
        self.assertEqual(len(self._history(card)) - before, 1,
                         'wizard writes its own row; ours must stand down')

    # -- award gate ----------------------------------------------------

    def _import_purchase(self, points=100.0):
        return self.env['mint.dutchie.purchase'].create({
            'partner_id': self.partner.id,
            'company_id': self.company.id,
            'receipt_no': 'AUDIT-%s' % points,
            'date': '2026-08-10',
            'net_total': points,
            'loyalty_points': points,
        })

    def _balance(self):
        card = self.env['loyalty.card'].search([
            ('partner_id', '=', self.partner.id),
            ('program_id', '=', self.program.id)], limit=1)
        return card.points if card else 0.0

    def test_award_gate_off_mints_nothing(self):
        self.env['ir.config_parameter'].sudo().set_param(AWARD_MODE_PARAM, 'off')
        before = self._balance()
        self._import_purchase(100.0)
        self.assertEqual(self._balance(), before,
                         'gate off: purchase imports but mints no points')

    def test_award_gate_defaults_to_off(self):
        self.env['ir.config_parameter'].sudo().search(
            [('key', '=', AWARD_MODE_PARAM)]).unlink()
        self.assertFalse(self.env['loyalty.card']._mint_awards_enabled(),
                         'an unset gate must fail closed, not open')

    def test_award_gate_legacy_still_mints(self):
        self.env['ir.config_parameter'].sudo().set_param(AWARD_MODE_PARAM, 'legacy')
        before = self._balance()
        self._import_purchase(60.0)
        self.assertEqual(self._balance(), before + 60.0,
                         'gate legacy: previous behaviour is restorable')

    def test_legacy_award_is_audited(self):
        self.env['ir.config_parameter'].sudo().set_param(AWARD_MODE_PARAM, 'legacy')
        self._import_purchase(35.0)
        card = self.env['loyalty.card'].search([
            ('partner_id', '=', self.partner.id),
            ('program_id', '=', self.program.id)], limit=1)
        self.assertIn('dutchie purchase import',
                      self._history(card).sorted('id')[-1].description,
                      'awarded points carry their receipt in the audit row')
