# -*- coding: utf-8 -*-
"""_upsert_order identifier backfill on existing orders.

Traces to a production gap: an order first created by a sync pass that lacked
the Dutchie checkout reference (e.g. the transaction backfill, keyed by
receipt only) never received dutchie_checkout_id from later syncs that DID
carry it. The update path backfilled receipt/shipment/order-number but not
checkout_id, so customer-side status polls (which look up by checkout id)
missed those orders forever.

The counterweight is the UNIQUE(company_id, dutchie_checkout_id) constraint:
a blind backfill would abort the whole bulk-sync batch whenever a parallel
lane-watcher stub already holds the id, so the backfill must probe for a
holder and skip (with a warning) instead of crashing.
"""
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.mint_pos_bridge.controllers.order_api import _upsert_order


@tagged('post_install', '-at_install')
class TestUpsertBackfill(TransactionCase):

    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.Order = self.env['mint.pos.order'].sudo()
        self.Line = self.env['mint.pos.order.line'].sudo()

    def _upsert(self, **order_data):
        order_data.setdefault('company_id', self.company.id)
        return _upsert_order(order_data, self.Order, self.Line,
                             allow_create=False)

    def test_backfills_checkout_id_on_receipt_matched_order(self):
        order = self.Order.create({
            'company_id': self.company.id,
            'state': 'completed',
            'dutchie_receipt_no': 'R-1001',
        })
        result = self._upsert(
            dutchie_receipt_no='R-1001',
            dutchie_checkout_id='555000111',
        )
        self.assertTrue(result['updated'])
        self.assertEqual(
            order.dutchie_checkout_id, '555000111',
            'A sync carrying the checkout id must stamp it onto an existing '
            'order that lacks one -- customer status polls look up by this id.',
        )

    def test_backfills_checkout_id_on_shipment_matched_order(self):
        order = self.Order.create({
            'company_id': self.company.id,
            'state': 'lobby',
            'dutchie_shipment_id': '888000222',
        })
        self._upsert(
            dutchie_shipment_id='888000222',
            dutchie_checkout_id='555000222',
        )
        self.assertEqual(order.dutchie_checkout_id, '555000222')

    def test_never_overwrites_existing_checkout_id(self):
        order = self.Order.create({
            'company_id': self.company.id,
            'state': 'lobby',
            'dutchie_receipt_no': 'R-1002',
            'dutchie_checkout_id': 'original-id',
        })
        self._upsert(
            dutchie_receipt_no='R-1002',
            dutchie_checkout_id='different-id',
        )
        self.assertEqual(
            order.dutchie_checkout_id, 'original-id',
            'Backfill is fill-only: a non-empty checkout id must never be '
            'clobbered by a later sync.',
        )

    def test_skips_backfill_when_another_order_holds_the_id(self):
        holder = self.Order.create({
            'company_id': self.company.id,
            'state': 'lobby',
            'dutchie_checkout_id': '555000333',
        })
        orphan = self.Order.create({
            'company_id': self.company.id,
            'state': 'completed',
            'dutchie_receipt_no': 'R-1003',
        })
        with self.assertLogs(
            'odoo.addons.mint_pos_bridge.controllers.order_api',
            level='WARNING',
        ) as captured:
            self._upsert(
                dutchie_receipt_no='R-1003',
                dutchie_checkout_id='555000333',
            )
        self.assertFalse(
            orphan.dutchie_checkout_id,
            'Backfilling an id another order holds would violate '
            'UNIQUE(company_id, dutchie_checkout_id) and abort the whole '
            'sync batch -- it must be skipped.',
        )
        self.assertEqual(holder.dutchie_checkout_id, '555000333')
        self.assertTrue(
            any('possible duplicate' in msg for msg in captured.output),
            'A held checkout id signals a duplicate order pair and must be '
            'logged for follow-up.',
        )

    def test_same_id_on_matched_order_is_not_flagged_as_duplicate(self):
        order = self.Order.create({
            'company_id': self.company.id,
            'state': 'lobby',
            'dutchie_checkout_id': '555000444',
            'dutchie_receipt_no': 'R-1004',
        })
        result = self._upsert(
            dutchie_receipt_no='R-1004',
            dutchie_checkout_id='555000444',
        )
        self.assertTrue(
            result['skipped'],
            'Re-syncing an order that already carries the same checkout id '
            'must be a no-op, not a duplicate warning.',
        )
        self.assertEqual(order.dutchie_checkout_id, '555000444')
