# -*- coding: utf-8 -*-
"""Tests for the DL blind index and its collision guard."""
from odoo.tests import TransactionCase, tagged

from ..models import pii_crypto


@tagged('post_install', '-at_install')
class TestDlBlindIndex(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Dev-fallback key so the derived pepper is available in tests.
        cls.env['ir.config_parameter'].sudo().set_param(
            pii_crypto.PARAM_KEY, 'Zj1kZXZfa2V5X2Zvcl90ZXN0c19vbmx5X18zMmJ5dGVzPQ==')

    def _partner(self, dl):
        return self.env['res.partner'].create({
            'name': 'Index Test %s' % dl,
            'x_dutchie_identity_key': 'dl:%s' % dl,
        })

    # -- normalisation --------------------------------------------------

    def test_normalisation_strips_punctuation(self):
        self.assertEqual(pii_crypto.normalize_dl('d-123 456'), 'D123456')

    def test_normalisation_rejects_empty(self):
        for bad in ('', None, '   ', '---'):
            self.assertEqual(pii_crypto.normalize_dl(bad), '')

    # -- the index itself ------------------------------------------------

    def test_index_is_deterministic(self):
        a = pii_crypto.blind_index(self.env, 'D02646007')
        b = pii_crypto.blind_index(self.env, 'D02646007')
        self.assertTrue(a)
        self.assertEqual(a, b, 'a join key that is not stable joins nothing')

    def test_index_ignores_punctuation_and_case(self):
        self.assertEqual(
            pii_crypto.blind_index(self.env, 'D-0264 6007'),
            pii_crypto.blind_index(self.env, 'd02646007'),
        )

    def test_index_differs_per_value(self):
        self.assertNotEqual(
            pii_crypto.blind_index(self.env, 'D02646007'),
            pii_crypto.blind_index(self.env, 'D02646008'),
        )

    def test_index_does_not_contain_the_plaintext(self):
        idx = pii_crypto.blind_index(self.env, 'D02646007')
        self.assertNotIn('02646007', idx)
        self.assertEqual(len(idx), 64, 'hex sha256')

    def test_domain_separation(self):
        self.assertNotEqual(
            pii_crypto.blind_index(self.env, 'X1', domain='dl'),
            pii_crypto.blind_index(self.env, 'X1', domain='other'),
        )

    def test_unusable_input_returns_false(self):
        self.assertFalse(pii_crypto.blind_index(self.env, '  '))

    def test_fails_closed_without_a_key(self):
        """An unpeppered hash of a low-entropy DL is reversible — never emit one."""
        self.env['ir.config_parameter'].sudo().set_param(pii_crypto.PARAM_KEY, '')
        import os
        prior = os.environ.pop(pii_crypto.ENV_KEY, None)
        try:
            self.assertFalse(pii_crypto.blind_index(self.env, 'D02646007'))
        finally:
            if prior is not None:
                os.environ[pii_crypto.ENV_KEY] = prior

    # -- backfill + guard -------------------------------------------------

    def test_backfill_populates_and_is_idempotent(self):
        p = self._partner('D77777001')
        self.env['res.partner']._backfill_dl_index(batch=50, limit=50)
        p.invalidate_recordset()
        self.assertTrue(p.x_dl_index)
        first = p.x_dl_index
        self.env['res.partner']._backfill_dl_index(batch=50, limit=50)
        p.invalidate_recordset()
        self.assertEqual(p.x_dl_index, first, 'rerun must not churn existing rows')

    def test_backfill_skips_unusable_keys(self):
        p = self.env['res.partner'].create({
            'name': 'Junk key', 'x_dutchie_identity_key': 'dl:   '})
        self.env['res.partner']._backfill_dl_index(batch=50, limit=50)
        p.invalidate_recordset()
        self.assertFalse(p.x_dl_index)

    def test_punctuation_variants_collide_and_are_flagged(self):
        """The guard: two DLs differing only by punctuation must NOT auto-match."""
        a = self._partner('D8811-22')
        b = self._partner('D881122')
        self.env['res.partner']._backfill_dl_index(batch=50)
        a.invalidate_recordset(); b.invalidate_recordset()
        self.assertEqual(a.x_dl_index, b.x_dl_index, 'precondition: they collide')
        self.env['res.partner']._flag_ambiguous_dl_index()
        a.invalidate_recordset(); b.invalidate_recordset()
        self.assertTrue(a.x_dl_ambiguous)
        self.assertTrue(b.x_dl_ambiguous)

    def test_distinct_dls_are_not_flagged(self):
        p = self._partner('D99999123')
        self.env['res.partner']._backfill_dl_index(batch=50)
        self.env['res.partner']._flag_ambiguous_dl_index()
        p.invalidate_recordset()
        self.assertFalse(p.x_dl_ambiguous)

    def test_backfill_leaves_plaintext_intact(self):
        """Cutover is a separate, reversible step — never destroy the source here."""
        p = self._partner('D55501234')
        self.env['res.partner']._backfill_dl_index(batch=50)
        p.invalidate_recordset()
        self.assertEqual(p.x_dutchie_identity_key, 'dl:D55501234')
