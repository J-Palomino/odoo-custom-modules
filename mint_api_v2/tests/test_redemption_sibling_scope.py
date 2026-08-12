"""Sibling-template scope on loyalty redemptions.

Dutchie product ids (and SKUs) are per-location: the same physical product
carried by two stores in a region lives on two product.template rows with two
different dutchie_product_ids. A redemption is locked region-wide via
store_ids, but its product scope used to be the ONE template the customer slid
on /rewards — so the coupon was nominally valid at every store in the state
while its product restriction could only ever match at one of them. Measured
on prod 2026-08-12: AZ carried 2 such pairs, NV 4, and the /rewards grid shows
them as indistinguishable duplicate cards.

create_redemption now widens product_ids to the sibling group (same normalized
name + same brand, located at the region's stores), and
_find_matching_free_line consumes against any member. The payload builder in
mint_command_center resolves the per-store id at push time; its regression
suite covers that half.
"""
from odoo.tests import TransactionCase, tagged

from odoo.addons.mint_api_v2.models.mint_discount import _find_matching_free_line


def _ensure_location_field(env):
    """x_location_id is a DB-created field on prod (no module defines it)."""
    if 'x_location_id' not in env['product.template']._fields:
        env['ir.model.fields'].create({
            'name': 'x_location_id',
            'model': 'product.template',
            'model_id': env['ir.model']._get_id('product.template'),
            'ttype': 'char',
            'state': 'manual',
        })


@tagged('post_install', '-at_install')
class TestRedemptionSiblingScope(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        _ensure_location_field(cls.env)
        cls.Discount = cls.env['mint.discount']
        cls.Template = cls.env['product.template']

        cls.region = cls.env['mint.region'].create({
            'name': 'RSS Testland', 'code': 'RS', 'slug': 'rss-testland'})
        cls.store_a = cls.env['res.company'].create({
            'name': 'RSS Store A', 'is_dispensary': True,
            'dutchie_store_id': 'rss-uuid-a', 'region_id': cls.region.id})
        cls.store_b = cls.env['res.company'].create({
            'name': 'RSS Store B', 'is_dispensary': True,
            'dutchie_store_id': 'rss-uuid-b', 'region_id': cls.region.id})
        cls.stores = cls.store_a + cls.store_b

        cls.brand = cls.env['mint.brand'].create({'name': 'RSS Brand'})
        cls.slid = cls.Template.create({
            'name': 'RSS - Gummies - Peach (H) (100mg)', 'type': 'consu',
            'brand_id': cls.brand.id, 'dutchie_product_id': '90000001',
            'default_code': 'RSS-SKU-A', 'x_location_id': 'rss-uuid-a'})
        cls.sibling = cls.Template.create({
            # Case differs on purpose: two stores' imports disagree on it.
            'name': 'RSS - GUMMIES - Peach (H) (100mg)', 'type': 'consu',
            'brand_id': cls.brand.id, 'dutchie_product_id': '90000002',
            'default_code': 'RSS-SKU-B', 'x_location_id': 'rss-uuid-b'})

    def test_siblings_found_across_the_region(self):
        group = self.Discount._sibling_redeemable_templates(
            self.slid, self.stores)
        self.assertEqual(set(group.ids), {self.slid.id, self.sibling.id})

    def test_same_name_other_brand_is_not_a_sibling(self):
        other_brand = self.env['mint.brand'].create({'name': 'RSS Other'})
        impostor = self.Template.create({
            'name': self.slid.name, 'type': 'consu',
            'brand_id': other_brand.id, 'dutchie_product_id': '90000009',
            'x_location_id': 'rss-uuid-b'})
        group = self.Discount._sibling_redeemable_templates(
            self.slid, self.stores)
        self.assertNotIn(impostor.id, group.ids)

    def test_other_state_template_is_not_a_sibling(self):
        elsewhere = self.Template.create({
            'name': self.slid.name, 'type': 'consu',
            'brand_id': self.brand.id, 'dutchie_product_id': '90000010',
            'x_location_id': 'rss-uuid-elsewhere'})
        group = self.Discount._sibling_redeemable_templates(
            self.slid, self.stores)
        self.assertNotIn(elsewhere.id, group.ids)

    def test_like_metacharacters_do_not_widen_the_match(self):
        """A '%' in a product name must not wildcard other products in.

        This scope feeds a 100%-off coupon; a false positive gives the wrong
        product away free.
        """
        wildcard = self.Template.create({
            'name': 'RSS - 100% Live Resin (1g)', 'type': 'consu',
            'brand_id': self.brand.id, 'dutchie_product_id': '90000011',
            'x_location_id': 'rss-uuid-a'})
        lookalike = self.Template.create({
            'name': 'RSS - 100XYZ Live Resin (1g)', 'type': 'consu',
            'brand_id': self.brand.id, 'dutchie_product_id': '90000012',
            'x_location_id': 'rss-uuid-b'})
        group = self.Discount._sibling_redeemable_templates(
            wildcard, self.stores)
        self.assertEqual(group.ids, wildcard.ids)
        self.assertNotIn(lookalike.id, group.ids)

    def test_redemption_scope_is_the_sibling_group(self):
        partner = self.env['res.partner'].create({'name': 'RSS Customer'})
        rec = self.Discount.create_redemption(
            partner=partner, points_cost=150, product=self.slid,
            store=self.store_a)
        self.assertEqual(set(rec.product_ids.ids),
                         {self.slid.id, self.sibling.id})
        # The slid template stays the single "what was redeemed" record.
        self.assertEqual(rec.redemption_product_id, self.slid)
        self.assertEqual(set(rec.store_ids.ids), set(self.stores.ids))

    def test_consume_matches_the_sibling_stores_line(self):
        """An order at store B consumes a coupon slid from store A's card."""
        partner = self.env['res.partner'].create({'name': 'RSS Customer 2'})
        rec = self.Discount.create_redemption(
            partner=partner, points_cost=150, product=self.slid,
            store=self.store_a)
        line = {'dutchie_product_id': '90000002', 'sku': 'RSS-SKU-B',
                'unit_price': 18.0, 'quantity': 1, 'discount': 18.0}
        self.assertIsNotNone(_find_matching_free_line(rec, [line]))
        consumed = self.Discount.consume_pending_redemption(
            partner, order_items=[line], store=self.store_b)
        self.assertEqual(consumed, rec)
        self.assertEqual(rec.redemption_status, 'used')

    def test_consume_still_rejects_unrelated_lines(self):
        partner = self.env['res.partner'].create({'name': 'RSS Customer 3'})
        rec = self.Discount.create_redemption(
            partner=partner, points_cost=150, product=self.slid,
            store=self.store_a)
        line = {'dutchie_product_id': '99999999', 'sku': 'RSS-SKU-ZZ',
                'unit_price': 18.0, 'quantity': 1, 'discount': 18.0}
        self.assertIsNone(_find_matching_free_line(rec, [line]))
        consumed = self.Discount.consume_pending_redemption(
            partner, order_items=[line], store=self.store_b)
        self.assertFalse(consumed)
        self.assertEqual(rec.redemption_status, 'pending')
