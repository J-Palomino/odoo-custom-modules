"""mint.brand is a curated registry: only a deliberate create may add a brand.

Every integration (mintinvsvc's odooSync, the upload scripts) authenticates as
uid=2, which is NOT superuser -- so these run as ``base.user_admin`` to look
exactly like one. Before the gate, odooSync's exact-name upsert re-created
every brand a merge had tombstoned, and invented brands from product names.
"""
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from ..models.product_template import BRAND_CREATE_CTX


@tagged('post_install', '-at_install')
class TestBrandCreateGate(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.admin = cls.env.ref('base.user_admin')
        cls.Brand = cls.env['mint.brand'].with_user(cls.admin)

    def test_integration_create_is_refused(self):
        # The shape of odooSync._resolveBrandIdNative: a bare create over RPC.
        with self.assertRaises(UserError):
            self.Brand.create({'name': 'Homiez - 28g Preroll Pack'})

    def test_quick_create_from_another_form_is_refused(self):
        with self.assertRaises(UserError):
            self.Brand.name_create('Drip')

    def test_refusal_names_every_rejected_brand(self):
        with self.assertRaisesRegex(UserError, r'Dr\. Dabber_, LUX'):
            self.Brand.create([{'name': 'Dr. Dabber_'}, {'name': 'LUX'}])

    def test_deliberate_create_is_allowed(self):
        brand = self.Brand.with_context(**{BRAND_CREATE_CTX: True}).create(
            {'name': 'Gate Opt-In'})
        self.assertTrue(brand.exists())

    def test_superuser_create_is_allowed(self):
        # Tests, migrations and odoo-bin shell run as superuser.
        brand = self.env['mint.brand'].create({'name': 'Gate Superuser'})
        self.assertTrue(brand.exists())

    def test_existing_brands_stay_editable(self):
        # The gate is create-only: renames, logos and banners still save.
        brand = self.env['mint.brand'].create({'name': 'Gate Edit'})
        brand.with_user(self.admin).write({'name': 'Gate Edited'})
        self.assertEqual(brand.name, 'Gate Edited')
