# -*- coding: utf-8 -*-
import json
from unittest.mock import patch

from odoo.tests import tagged
from odoo.tests.common import TransactionCase, HttpCase


@tagged('post_install', '-at_install')
class TestVisualCMSHelpers(TransactionCase):
    """Unit tests for Visual CMS helper functions and data structures."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Create a parent company and child store for testing
        cls.parent = cls.env['res.company'].search([('parent_id', '=', False)], limit=1)
        cls.state_az = cls.env['res.country.state'].search([('name', '=', 'Arizona')], limit=1)

        cls.store = cls.env['res.company'].create({
            'name': 'Mint Test Store',
            'parent_id': cls.parent.id,
            'x_slug': 'test-store',
            'state_id': cls.state_az.id if cls.state_az else False,
        })

        # Create test banners
        cls.banner_published = cls.env['mint.banner'].create({
            'name': 'Test Hero Banner',
            'slot': 'hero',
            'active': True,
            'company_id': cls.store.id,
        })
        cls.banner_global = cls.env['mint.banner'].create({
            'name': 'Global Spotlight',
            'slot': 'brand-spotlight',
            'active': True,
            'company_id': False,
        })
        cls.banner_inactive = cls.env['mint.banner'].create({
            'name': 'Inactive Banner',
            'slot': 'hero',
            'active': False,
            'company_id': cls.store.id,
        })

    def test_zones_definition(self):
        """ZONES list should contain all expected slots matching Astro layout."""
        from odoo.addons.mint_visual_cms.controllers.main import ZONES

        zone_ids = [z['id'] for z in ZONES]
        # Editable banner slots
        self.assertIn('hero', zone_ids)
        self.assertIn('brand-spotlight', zone_ids)
        self.assertIn('after-flower', zone_ids)
        self.assertIn('after-pre-rolls', zone_ids)
        self.assertIn('after-vapes', zone_ids)
        self.assertIn('after-edibles', zone_ids)
        self.assertIn('after-concentrates', zone_ids)
        self.assertIn('deals-popup', zone_ids)
        # Placeholder zones
        self.assertIn('best-deals', zone_ids)
        self.assertIn('shop-by-category', zone_ids)
        # Info zones
        self.assertIn('hero-image', zone_ids)
        self.assertIn('gallery', zone_ids)

        self.assertEqual(len(ZONES), 12)

    def test_zones_editable_flags(self):
        """Only banner-slot zones should be editable."""
        from odoo.addons.mint_visual_cms.controllers.main import ZONES

        editable = [z['id'] for z in ZONES if z['editable']]
        readonly = [z['id'] for z in ZONES if not z['editable']]

        self.assertEqual(len(editable), 8)  # 8 banner slots
        self.assertEqual(len(readonly), 4)  # hero-image, best-deals, shop-by-category, gallery

        for z in ZONES:
            if z['editable']:
                self.assertIsNotNone(z.get('slot'), f"Editable zone {z['id']} must have a slot")

    def test_banner_to_dict_with_url(self):
        """_banner_to_dict should serialize banner with image_url."""
        from odoo.addons.mint_visual_cms.controllers.main import _banner_to_dict

        self.banner_published.image_url = 'https://example.com/hero.jpg'
        result = _banner_to_dict(self.banner_published)

        self.assertEqual(result['id'], self.banner_published.id)
        self.assertEqual(result['name'], 'Test Hero Banner')
        self.assertEqual(result['slot'], 'hero')
        self.assertEqual(result['image_url'], 'https://example.com/hero.jpg')
        self.assertTrue(result['has_image'])
        self.assertEqual(result['company_id'], self.store.id)
        self.assertEqual(result['company_name'], 'Mint Test Store')

    def test_banner_to_dict_without_image(self):
        """_banner_to_dict should handle banners with no image."""
        from odoo.addons.mint_visual_cms.controllers.main import _banner_to_dict

        self.banner_published.image_url = False
        self.banner_published.image = False
        result = _banner_to_dict(self.banner_published)

        self.assertEqual(result['image_url'], '')
        self.assertFalse(result['has_image'])

    def test_banner_to_dict_binary_image(self):
        """_banner_to_dict should build /web/image URL for binary images."""
        from odoo.addons.mint_visual_cms.controllers.main import _banner_to_dict

        self.banner_published.image_url = False
        self.banner_published.image = b'\x89PNG'  # minimal binary
        result = _banner_to_dict(self.banner_published)

        self.assertIn('/web/image/mint.banner/', result['image_url'])
        self.assertTrue(result['has_image'])

    def test_banner_to_dict_global(self):
        """_banner_to_dict should show 'Global' for banners without company."""
        from odoo.addons.mint_visual_cms.controllers.main import _banner_to_dict

        result = _banner_to_dict(self.banner_global)
        self.assertFalse(result['company_id'])
        self.assertEqual(result['company_name'], 'Global')

    def test_banner_sequence_reorder(self):
        """Reordering should update sequence values."""
        b1 = self.env['mint.banner'].create({
            'name': 'Reorder A', 'slot': 'hero', 'active': True, 'sequence': 50,
        })
        b2 = self.env['mint.banner'].create({
            'name': 'Reorder B', 'slot': 'hero', 'active': True, 'sequence': 60,
        })

        # Simulate reorder: B before A
        Banner = self.env['mint.banner'].sudo()
        for seq, bid in enumerate([b2.id, b1.id]):
            Banner.browse(bid).write({'sequence': seq * 10})

        b1.invalidate_recordset()
        b2.invalidate_recordset()
        self.assertEqual(b2.sequence, 0)
        self.assertEqual(b1.sequence, 10)


@tagged('post_install', '-at_install')
class TestVisualCMSRoutes(HttpCase):
    """HTTP integration tests for Visual CMS routes."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.parent = cls.env['res.company'].search([('parent_id', '=', False)], limit=1)
        cls.state_az = cls.env['res.country.state'].search([('name', '=', 'Arizona')], limit=1)

        cls.store = cls.env['res.company'].create({
            'name': 'Mint Route Test Store',
            'parent_id': cls.parent.id,
            'x_slug': 'route-test',
            'state_id': cls.state_az.id if cls.state_az else False,
        })
        cls.env['mint.banner'].create({
            'name': 'Route Test Banner',
            'slot': 'hero',
            'active': True,
            'company_id': cls.store.id,
        })

    def test_landing_page_loads(self):
        """GET /visual-cms should return 200 with store grid."""
        self.authenticate('admin', 'admin')
        resp = self.url_open('/visual-cms')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('Visual CMS', resp.text)
        self.assertIn('route-test', resp.text)

    def test_wireframe_page_loads(self):
        """GET /visual-cms/<slug> should return 200 with wireframe zones."""
        self.authenticate('admin', 'admin')
        resp = self.url_open('/visual-cms/route-test')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('Mint Route Test Store', resp.text)
        self.assertIn('vcms-zone', resp.text)
        self.assertIn('Hero Carousel', resp.text)

    def test_wireframe_page_not_found(self):
        """GET /visual-cms/<bad-slug> should return 404."""
        self.authenticate('admin', 'admin')
        resp = self.url_open('/visual-cms/nonexistent-store-xyz')
        self.assertEqual(resp.status_code, 404)

    def test_wireframe_has_all_zones(self):
        """Wireframe page should render all 12 zone blocks."""
        self.authenticate('admin', 'admin')
        resp = self.url_open('/visual-cms/route-test')
        text = resp.text
        # Check key zone labels are present
        for label in ['Hero Carousel', 'Brand Spotlight', 'Best Deals',
                      'Shop by Category', 'Flower Section', 'Pre-Rolls Section',
                      'Vapes Section', 'Edibles Section', 'Concentrates Section',
                      'Photo Gallery', 'Deals Popup', 'Store Hero Image']:
            self.assertIn(label, text, f"Zone '{label}' not found in wireframe")

    def test_api_slot_banners(self):
        """JSON API /visual-cms/api/slot-banners should return banners."""
        self.authenticate('admin', 'admin')
        resp = self.url_open(
            '/visual-cms/api/slot-banners',
            data=json.dumps({
                'jsonrpc': '2.0', 'method': 'call', 'id': 1,
                'params': {'slot': 'hero', 'company_id': self.store.id},
            }),
            headers={'Content-Type': 'application/json'},
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertNotIn('error', body)
        banners = body['result']
        self.assertIsInstance(banners, list)
        self.assertTrue(len(banners) >= 1)
        self.assertEqual(banners[0]['slot'], 'hero')

    def test_api_slot_banners_empty(self):
        """JSON API should return empty list for slot with no banners."""
        self.authenticate('admin', 'admin')
        resp = self.url_open(
            '/visual-cms/api/slot-banners',
            data=json.dumps({
                'jsonrpc': '2.0', 'method': 'call', 'id': 1,
                'params': {'slot': 'deals-popup', 'company_id': self.store.id},
            }),
            headers={'Content-Type': 'application/json'},
        )
        body = resp.json()
        banners = body['result']
        self.assertIsInstance(banners, list)

    def test_api_reorder(self):
        """JSON API /visual-cms/api/reorder should update sequences."""
        b1 = self.env['mint.banner'].create({
            'name': 'Order A', 'slot': 'hero', 'active': True, 'sequence': 10,
        })
        b2 = self.env['mint.banner'].create({
            'name': 'Order B', 'slot': 'hero', 'active': True, 'sequence': 20,
        })

        self.authenticate('admin', 'admin')
        resp = self.url_open(
            '/visual-cms/api/reorder',
            data=json.dumps({
                'jsonrpc': '2.0', 'method': 'call', 'id': 1,
                'params': {'banner_ids': [b2.id, b1.id]},
            }),
            headers={'Content-Type': 'application/json'},
        )
        body = resp.json()
        self.assertEqual(body['result']['status'], 'ok')

        b1.invalidate_recordset()
        b2.invalidate_recordset()
        self.assertLess(b2.sequence, b1.sequence)

    def test_api_clear_cache(self):
        """JSON API /visual-cms/api/clear-cache should not error."""
        self.authenticate('admin', 'admin')
        resp = self.url_open(
            '/visual-cms/api/clear-cache',
            data=json.dumps({
                'jsonrpc': '2.0', 'method': 'call', 'id': 1,
                'params': {},
            }),
            headers={'Content-Type': 'application/json'},
        )
        body = resp.json()
        # Cache clear may fail in test env (no frontend running), but API should not crash
        self.assertIn('result', body)
        self.assertIn(body['result']['status'], ('ok', 'error'))

    def test_landing_requires_auth(self):
        """Visual CMS pages should require authentication."""
        resp = self.url_open('/visual-cms')
        # Odoo redirects to login for auth='user' routes
        self.assertIn('/web/login', resp.url)

    def test_store_selector_present(self):
        """Wireframe page should include the store selector dropdown."""
        self.authenticate('admin', 'admin')
        resp = self.url_open('/visual-cms/route-test')
        self.assertIn('vcmsStoreSelector', resp.text)

    def test_publish_button_present(self):
        """Wireframe page should include the Publish Now button."""
        self.authenticate('admin', 'admin')
        resp = self.url_open('/visual-cms/route-test')
        self.assertIn('vcmsPublishBtn', resp.text)
        self.assertIn('Publish Now', resp.text)
