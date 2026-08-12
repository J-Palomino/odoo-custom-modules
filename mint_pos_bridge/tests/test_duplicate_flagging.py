# -*- coding: utf-8 -*-
"""Tests for duplicate-partner flagging (flag, never merge)."""
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestDuplicateFlagging(TransactionCase):

    def _p(self, name, **kw):
        return self.env['res.partner'].create({'name': name, **kw})

    def test_flags_pointer_and_review(self):
        orphan, canonical = self._p('Orphan A'), self._p('Canonical A')
        res = self.env['res.partner']._flag_duplicate_partners(
            [(orphan.id, canonical.id, 'dutchie id 123 via receipt 999')])
        self.assertEqual(res['flagged'], 1)
        orphan.invalidate_recordset()
        self.assertEqual(orphan.x_duplicate_of_id, canonical)
        self.assertTrue(orphan.x_merge_needs_review)
        self.assertIn('123', orphan.x_duplicate_evidence)

    def test_never_writes_a_customer_id(self):
        """The whole point: x_dutchie_customer_id is UNIQUE, so a batch job
        must not attempt to stamp it onto a duplicate."""
        canonical = self._p('Canonical B', x_dutchie_customer_id='90000001')
        orphan = self._p('Orphan B')
        self.env['res.partner']._flag_duplicate_partners(
            [(orphan.id, canonical.id, 'evidence')])
        orphan.invalidate_recordset()
        self.assertFalse(orphan.x_dutchie_customer_id,
                         'flagging must not claim the canonical id')
        canonical.invalidate_recordset()
        self.assertEqual(canonical.x_dutchie_customer_id, '90000001',
                         'canonical record is left untouched')

    def test_nothing_is_merged(self):
        orphan, canonical = self._p('Orphan C'), self._p('Canonical C')
        self.env['res.partner']._flag_duplicate_partners(
            [(orphan.id, canonical.id, 'e')])
        self.assertTrue(orphan.exists(), 'the duplicate must still exist')
        self.assertTrue(canonical.exists())
        orphan.invalidate_recordset()
        self.assertEqual(orphan.name, 'Orphan C', 'no field was rewritten')

    def test_skips_claimed_accounts(self):
        """A partner with a login is a real account — don't relabel it."""
        canonical = self._p('Canonical D')
        orphan = self._p('Orphan D', email='dup-test@example.com')
        self.env['res.users'].create({
            'login': 'dup-test@example.com', 'partner_id': orphan.id})
        res = self.env['res.partner']._flag_duplicate_partners(
            [(orphan.id, canonical.id, 'e')])
        self.assertEqual(res['skipped'], 1)
        self.assertEqual(res['flagged'], 0)
        orphan.invalidate_recordset()
        self.assertFalse(orphan.x_merge_needs_review)

    def test_skips_self_pairs(self):
        p = self._p('Self')
        res = self.env['res.partner']._flag_duplicate_partners([(p.id, p.id, 'e')])
        self.assertEqual(res['flagged'], 0)

    def test_is_idempotent(self):
        orphan, canonical = self._p('Orphan E'), self._p('Canonical E')
        pairs = [(orphan.id, canonical.id, 'e')]
        self.env['res.partner']._flag_duplicate_partners(pairs)
        second = self.env['res.partner']._flag_duplicate_partners(pairs)
        self.assertEqual(second['flagged'], 1, 'rerun rewrites the same values')
        orphan.invalidate_recordset()
        self.assertEqual(orphan.x_duplicate_of_id, canonical)

    def test_review_action_domain_finds_flagged(self):
        orphan, canonical = self._p('Orphan F'), self._p('Canonical F')
        self.env['res.partner']._flag_duplicate_partners(
            [(orphan.id, canonical.id, 'e')])
        found = self.env['res.partner'].search([('x_merge_needs_review', '=', True)])
        self.assertIn(orphan, found, 'the review screen must surface it')
