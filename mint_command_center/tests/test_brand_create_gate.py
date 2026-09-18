"""mint.brand's create gate, seen from the two mint_command_center paths it touches.

- The Brands menu is the one UI path allowed past the gate. If the action ever
  loses its context flag, marketing silently loses "New" on the Brands list.
- ptlDealsRefresh (mintinvsvc) createBatch()es ~8k deals a day as uid=2, and
  each create fires the stored brand compute. That compute must only MATCH
  brands -- if it ever tried to create one again, the gate would fail the
  whole store's batch and blank its daily-deals page.
"""
from odoo.tests import TransactionCase, tagged
from odoo.tools.safe_eval import safe_eval

from odoo.addons.mint_api_v2.models.product_template import BRAND_CREATE_CTX


@tagged('post_install', '-at_install')
class TestBrandCreateGate(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # uid=2 is what every integration authenticates as: not superuser.
        cls.admin = cls.env.ref('base.user_admin')
        cls.admin.group_ids = [(4, cls.env.ref('mint_command_center.group_ptl_manager').id)]
        cls.Brand = cls.env['mint.brand']

    def test_brands_action_opts_in_to_create(self):
        action = self.env.ref('mint_command_center.action_mint_brand')
        ctx = safe_eval(action.context or '{}')
        self.assertTrue(ctx.get(BRAND_CREATE_CTX))
        # ...and that context really does get a non-superuser past the gate.
        brand = self.Brand.with_user(self.admin).with_context(**ctx).create(
            {'name': 'From the Brands menu'})
        self.assertTrue(brand.exists())

    def test_deal_with_unmatched_brand_is_created_unbranded(self):
        before = self.Brand.search_count([])
        deal = self.env['mint.ptl.deal'].with_user(self.admin).create(
            {'name': 'Zzq Nonexistent Brand - 1g Cart'})
        self.assertFalse(deal.brand_id)
        self.assertEqual(self.Brand.search_count([]), before)

    def test_deal_with_known_brand_still_links_it(self):
        brand = self.Brand.create({'name': 'Zzq Known Brand'})
        deal = self.env['mint.ptl.deal'].with_user(self.admin).create(
            {'name': 'Zzq Known Brand - 1g Cart'})
        self.assertEqual(deal.brand_id, brand)
