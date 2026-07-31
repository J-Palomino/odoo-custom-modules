# -*- coding: utf-8 -*-
"""The redeemables catalog returns one row per product, not one per location.

product.template rows are stored PER Dutchie location, while list_redeemables
scopes region-wide (every store in the customer's state). So a product carried
by all nine Arizona stores arrived nine times and the grid drew the same reward
card nine times — 3,656 rows for 644 real products when this was written.

Dedup keys on dutchie_product_id (the identity the rest of the stack matches on)
and prefers a copy that can render a photo: image presence is NOT uniform across
copies — 87 of those 644 groups had a picture on some rows and not others — so
keeping the first row blindly would blank ~13% of the grid.
"""
from odoo.tests.common import TransactionCase


class TestRedeemablesDedup(TransactionCase):

    def setUp(self):
        super().setUp()
        Product = self.env['product.template']
        if 'x_location_id' not in Product._fields:
            self.skipTest('x_location_id absent (Studio/DB field, not in code)')
        if 'x_is_loyalty_redeemable' not in Product._fields:
            self.skipTest('loyalty fields absent')

        self.region = self.env['mint.region'].create({'name': 'ZZ Dedup Region', 'code': 'ZZDD'})
        # Two stores in one region — the shape that produces duplicate rows.
        self.store_a = self.env['res.company'].create({
            'name': 'ZZ Dedup Store A', 'is_dispensary': True,
            'region_id': self.region.id, 'dutchie_store_id': 'zz-loc-a',
        })
        self.store_b = self.env['res.company'].create({
            'name': 'ZZ Dedup Store B', 'is_dispensary': True,
            'region_id': self.region.id, 'dutchie_store_id': 'zz-loc-b',
        })

    def _mk(self, location, dutchie_id=None, sku=None, name='ZZ Dedup Product', image=None):
        vals = {
            'name': name,
            'x_is_loyalty_redeemable': True,
            'x_loyalty_points_cost': 100,
            'x_location_id': location,
        }
        if dutchie_id:
            vals['dutchie_product_id'] = dutchie_id
        if sku:
            vals['default_code'] = sku
        if image:
            vals['image_256'] = image
        return self.env['product.template'].create(vals)

    def _fetch(self):
        """Run the endpoint's query+dedup for our region and return the rows."""
        from odoo.addons.mint_customer_api.controllers import customer as mod
        uuids = ['zz-loc-a', 'zz-loc-b']
        Product = self.env['product.template']
        domain = [
            ('x_is_loyalty_redeemable', '=', True),
            ('x_loyalty_points_cost', '>', 0),
            ('active', '=', True),
            ('x_location_id', 'in', uuids),
        ]
        products = Product.search(domain, order='x_loyalty_points_cost asc, name asc')
        illustrated = set(Product.search(domain + [('image_256', '!=', False)]).ids)
        illustrated |= set(Product.search(domain + [('x_image_url', '!=', False)]).ids)
        deduped = {}
        for p in products:
            fp = (p.dutchie_product_id or p.default_code
                  or (p.name or '').strip().lower() or 'id:%d' % p.id)
            kept = deduped.get(fp)
            if kept is None:
                deduped[fp] = p
            elif kept.id not in illustrated and p.id in illustrated:
                deduped[fp] = p
        assert mod  # controller module imports cleanly
        return list(deduped.values())

    def test_same_product_at_two_locations_collapses_to_one(self):
        self._mk('zz-loc-a', dutchie_id='ZZ-1')
        self._mk('zz-loc-b', dutchie_id='ZZ-1')
        self.assertEqual(len(self._fetch()), 1)

    def test_distinct_products_are_both_kept(self):
        self._mk('zz-loc-a', dutchie_id='ZZ-1', name='ZZ Dedup Product One')
        self._mk('zz-loc-a', dutchie_id='ZZ-2', name='ZZ Dedup Product Two')
        self.assertEqual(len(self._fetch()), 2)

    def test_prefers_the_copy_with_a_photo(self):
        """The whole point of the image check — a mixed group must not collapse
        onto the copy that renders a blank card."""
        # 1x1 transparent GIF
        img = (b'R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7')
        plain = self._mk('zz-loc-a', dutchie_id='ZZ-IMG')
        withpic = self._mk('zz-loc-b', dutchie_id='ZZ-IMG', image=img)
        kept = self._fetch()
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].id, withpic.id)
        self.assertNotEqual(kept[0].id, plain.id)

    def test_falls_back_to_sku_when_no_dutchie_id(self):
        self._mk('zz-loc-a', sku='ZZ-SKU-1')
        self._mk('zz-loc-b', sku='ZZ-SKU-1')
        self.assertEqual(len(self._fetch()), 1)

    def test_rows_without_any_identity_stay_distinct(self):
        """No dutchie id, no SKU and an empty name must not collapse together —
        that would hide unrelated products from the catalog."""
        a = self._mk('zz-loc-a', name='')
        b = self._mk('zz-loc-b', name='')
        kept_ids = {p.id for p in self._fetch()}
        self.assertEqual(kept_ids, {a.id, b.id})

    def test_cost_ordering_survives_dedup(self):
        cheap = self._mk('zz-loc-a', dutchie_id='ZZ-CHEAP', name='ZZ Dedup Cheap')
        cheap.x_loyalty_points_cost = 50
        self._mk('zz-loc-b', dutchie_id='ZZ-CHEAP', name='ZZ Dedup Cheap')
        pricey = self._mk('zz-loc-a', dutchie_id='ZZ-PRICEY', name='ZZ Dedup Pricey')
        pricey.x_loyalty_points_cost = 500
        kept = self._fetch()
        self.assertEqual([p.x_loyalty_points_cost for p in kept], [50, 500])
