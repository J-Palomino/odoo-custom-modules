# -*- coding: utf-8 -*-
"""Regression tests for cart store_id resolution.

The FE keys stores by their Dutchie store UUID (res.company.dutchie_store_id),
but carts are keyed by the integer company_id. Before the fix, the controller
did a bare int(store_id), so every authenticated cart call from the FE (which
sends the UUID) raised ValueError -> 400. These tests pin the dual-form
resolution so that regression can't silently return.
"""
from odoo.tests.common import TransactionCase
from odoo.addons.mint_account.controllers.cart import _resolve_store_id


class TestCartStoreIdResolution(TransactionCase):

    def setUp(self):
        super().setUp()
        self.company = self.env['res.company'].search([], limit=1)
        # dutchie_store_id is contributed by mint_api_v2 (declared dependency).
        self.company.dutchie_store_id = 'test-uuid-abc-123'

    def test_numeric_company_id_passthrough(self):
        """A numeric company id (int or str) is returned as-is, no lookup."""
        self.assertEqual(_resolve_store_id(self.env, self.company.id), self.company.id)
        self.assertEqual(_resolve_store_id(self.env, str(self.company.id)), self.company.id)

    def test_dutchie_uuid_resolves_to_company_id(self):
        """A Dutchie store UUID resolves to the owning company's int id."""
        self.assertEqual(
            _resolve_store_id(self.env, 'test-uuid-abc-123'), self.company.id
        )

    def test_unresolvable_uuid_returns_none(self):
        """An unknown UUID resolves to None (callers turn this into a 400)."""
        self.assertIsNone(_resolve_store_id(self.env, 'no-such-store-uuid'))

    def test_blank_and_none_return_none(self):
        self.assertIsNone(_resolve_store_id(self.env, None))
        self.assertIsNone(_resolve_store_id(self.env, ''))
        self.assertIsNone(_resolve_store_id(self.env, '   '))
