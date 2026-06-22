"""Regression: dutchie.publish.mode normalization (#4 fail-closed).

Path-1 previously treated any value that wasn't 'off'/'dry_run' as LIVE
(fail-open). normalize_publish_mode coerces an unrecognized value to 'off' so a
typo'd or unexpected mode can never publish to real Dutchie, while preserving
the historical default (unset -> 'dry_run').
"""
from odoo.tests import TransactionCase, tagged

from ..models.deal_mixins import normalize_publish_mode


@tagged('post_install', '-at_install')
class TestPublishMode(TransactionCase):

    def test_known_modes_passthrough(self):
        self.assertEqual(normalize_publish_mode('off'), 'off')
        self.assertEqual(normalize_publish_mode('dry_run'), 'dry_run')
        self.assertEqual(normalize_publish_mode('live'), 'live')
        self.assertEqual(normalize_publish_mode('  LIVE  '), 'live')  # trimmed + lowered

    def test_unset_uses_default(self):
        self.assertEqual(normalize_publish_mode(None), 'dry_run')
        self.assertEqual(normalize_publish_mode(''), 'dry_run')
        self.assertEqual(normalize_publish_mode('   '), 'dry_run')

    def test_unknown_fails_closed_to_off(self):
        # the #4 bug: anything unrecognized must NOT go live
        self.assertEqual(normalize_publish_mode('on'), 'off')
        self.assertEqual(normalize_publish_mode('dryrun'), 'off')
        self.assertEqual(normalize_publish_mode('LIVE!'), 'off')
        self.assertEqual(normalize_publish_mode('true'), 'off')
