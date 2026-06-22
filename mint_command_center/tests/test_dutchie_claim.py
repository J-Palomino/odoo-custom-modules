"""Regression: cross-path Dutchie publish mutex (#2 stopgap).

Two paths publish a deal to Dutchie — the submission convert auto-publish and
the PTL Publish button — with different ExternalIds/topologies, so both firing
makes a DUPLICATE live discount. The mutex: first live writer claims the deal
(mint.ptl.deal.dutchie_publish_owner); the other path is then blocked from
creating a parallel record; same-path re-publish stays allowed.

Exercises the pure decision ``dutchie_claim_decision`` (used by
``mint.ptl.deal._dutchie_claim``), so no Odoo fixtures are needed.
"""
from odoo.tests import TransactionCase, tagged

from ..models.deal_mixins import dutchie_claim_decision


@tagged('post_install', '-at_install')
class TestDutchieClaim(TransactionCase):

    def test_first_writer_claims(self):
        self.assertEqual(dutchie_claim_decision(False, 'submission'), (True, 'submission'))
        self.assertEqual(dutchie_claim_decision(None, 'ptl'), (True, 'ptl'))

    def test_same_path_republish_allowed_no_restamp(self):
        # owner already this path -> may publish, owner unchanged (None)
        self.assertEqual(dutchie_claim_decision('submission', 'submission'), (True, None))
        self.assertEqual(dutchie_claim_decision('ptl', 'ptl'), (True, None))

    def test_other_path_blocked(self):
        self.assertEqual(dutchie_claim_decision('submission', 'ptl'), (False, None))
        self.assertEqual(dutchie_claim_decision('ptl', 'submission'), (False, None))
