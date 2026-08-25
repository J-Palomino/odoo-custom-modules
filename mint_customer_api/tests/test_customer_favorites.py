# -*- coding: utf-8 -*-
"""Favorites are unique per (partner, item_type, item_ref).

The storefront hearts are idempotent by design: double-tapping must not create
a second row, and the same reference under a different item_type is a genuinely
different favorite (a product and a deal can share a numeric id — they come
from different Dutchie tables).

`location_id` is deliberately NOT part of the key: hearting the same product
at two stores is one favorite, so a customer who switches their selected store
does not silently accumulate duplicates.
"""
from psycopg2 import IntegrityError

from odoo.tests.common import TransactionCase
from odoo.tools import mute_logger


class TestCustomerFavorites(TransactionCase):

    def setUp(self):
        super().setUp()
        self.partner = self.env['res.partner'].create({
            'name': 'ZZ Favorites Test Customer',
            'email': 'zz-favorites-uniq@example.test',
            'is_web_customer': True,
        })
        self.Favorite = self.env['mint.customer.favorite']

    def _fav(self, **overrides):
        vals = {
            'partner_id': self.partner.id,
            'item_type': 'product',
            'item_ref': '13815543',
        }
        vals.update(overrides)
        return self.Favorite.create(vals)

    def test_duplicate_same_type_and_ref_is_rejected(self):
        self._fav()
        with self.assertRaises(IntegrityError), mute_logger('odoo.sql_db'):
            with self.cr.savepoint():
                self._fav()

    def test_same_ref_different_type_is_allowed(self):
        """A product_id and a discount_id may collide numerically."""
        self._fav()
        deal = self._fav(item_type='deal')
        self.assertTrue(deal.id)
        self.assertEqual(
            self.Favorite.search_count([('partner_id', '=', self.partner.id)]), 2,
        )

    def test_location_is_not_part_of_identity(self):
        """Same product favorited at a second store is still one favorite."""
        self._fav(location_id='f8dbdb94-7bea-4741-a3a9-6631d8430544')
        with self.assertRaises(IntegrityError), mute_logger('odoo.sql_db'):
            with self.cr.savepoint():
                self._fav(location_id='a4d8b494-b542-4f69-acb8-cf67d6a6c3aa')

    def test_a_store_can_be_favorited_alongside_a_product(self):
        """Stores key on the res.company id, not a Dutchie SKU."""
        self._fav()
        store = self._fav(item_type='store', item_ref='18', label='AZ - Mesa')
        self.assertTrue(store.id)
        self.assertEqual(
            self.Favorite.search_count([
                ('partner_id', '=', self.partner.id),
                ('item_type', '=', 'store'),
            ]),
            1,
        )

    def test_the_same_store_cannot_be_favorited_twice(self):
        self._fav(item_type='store', item_ref='18')
        with self.assertRaises(IntegrityError), mute_logger('odoo.sql_db'):
            with self.cr.savepoint():
                self._fav(item_type='store', item_ref='18')

    def test_favorites_follow_the_partner_on_delete(self):
        fav = self._fav()
        self.partner.unlink()
        self.assertFalse(fav.exists())
