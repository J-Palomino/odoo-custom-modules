# -*- coding: utf-8 -*-
"""mint.pos.order.line brand is filled from the product on create.

Traces to a production gap: no Dutchie order feed carries brand. Report 1082
(register walk-ins, including every employee sample) has no brand column and
POS-API transaction items have none, so 4.8M of 4.8M synced lines stored an
empty brand and the employee sample-review survey had nothing to prefill.
The line already carries the Dutchie product ID, so brand is resolved by ID.
"""
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestLineBrand(TransactionCase):

    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.Order = self.env['mint.pos.order'].sudo()
        self.Line = self.env['mint.pos.order.line'].sudo()
        Brand = self.env['mint.brand'].sudo()
        self.brand = Brand.create({'name': 'Test Brand A'})
        self.other = Brand.create({'name': 'Test Brand B'})
        self.tmpl = self.env['product.template'].sudo().create({
            'name': 'Brand A - Sample Employee - Flower - Test (3.5g)',
            'dutchie_product_id': '990001',
            'default_code': 'SKU-990001',
            'brand_id': self.brand.id,
        })
        self.sku_tmpl = self.env['product.template'].sudo().create({
            'name': 'Brand B - Vape - Test (1g)',
            'default_code': 'SKU-990002',
            'brand_id': self.other.id,
        })

    def _order(self, **line):
        line.setdefault('product_name', 'Test line')
        return self.Order.create({
            'company_id': self.company.id,
            'state': 'completed',
            'line_ids': [(0, 0, line)],
        })

    def test_fills_brand_from_dutchie_product_id(self):
        order = self._order(dutchie_product_id='990001')
        self.assertEqual(order.line_ids.brand, 'Test Brand A')

    def test_falls_back_to_sku(self):
        order = self._order(sku='SKU-990002')
        self.assertEqual(order.line_ids.brand, 'Test Brand B')

    def test_keeps_brand_the_feed_sent(self):
        order = self._order(dutchie_product_id='990001', brand='From Cart')
        self.assertEqual(
            order.line_ids.brand, 'From Cart',
            'A brand supplied by the source (web checkout) must not be overwritten.',
        )

    def test_unknown_product_leaves_brand_empty(self):
        order = self._order(dutchie_product_id='does-not-exist', sku='nope')
        self.assertFalse(order.line_ids.brand)

    def test_resolve_brands_covers_existing_lines(self):
        order = self._order(dutchie_product_id='990001')
        order.line_ids.write({'brand': False})
        self.assertEqual(
            order.line_ids._resolve_brands(), {order.line_ids.id: self.brand},
        )
