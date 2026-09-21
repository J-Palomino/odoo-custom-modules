"""Per-market deal banners must never touch another market's art.

The Missouri "Heady Background" drop (2026-09-18) covered 17 brands that
already had Arizona art in mint.brand.deal_banner -- the reason this model
exists. The R2 upload is patched out; the key it is called with is the contract.
"""
import base64
from unittest.mock import patch

from odoo.tests import TransactionCase, tagged
from odoo.tools import mute_logger

# Smallest valid PNG (1x1); _detect_image_type keys on magic bytes only.
PNG = base64.b64encode(
    b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06'
    b'\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01'
    b'\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82'
)
UPLOAD = 'odoo.addons.mint_api_v2.utils.r2_upload.upload_to_r2'
CDN = 'https://cdn.test/'


def fake_upload(image_bytes, key, content_type):
    return CDN + key


@tagged('post_install', '-at_install')
class TestBrandMarketBanner(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Region = cls.env['mint.region']
        cls.az = Region.create({'name': 'Arizona Test', 'code': 'AZ', 'slug': 'arizona'})
        cls.mo = Region.create({'name': 'Missouri Test', 'code': 'MO', 'slug': 'missouri'})
        cls.brand = cls.env['mint.brand'].create({'name': 'Wyld Test'})
        cls.Banner = cls.env['mint.brand.market.banner']

    def test_each_market_gets_its_own_cdn_object(self):
        with patch(UPLOAD, side_effect=fake_upload) as upload:
            mo = self.Banner.create({'brand_id': self.brand.id, 'region_id': self.mo.id, 'banner': PNG})
            az = self.Banner.create({'brand_id': self.brand.id, 'region_id': self.az.id, 'banner': PNG})
        keys = [c.args[1] for c in upload.call_args_list]
        self.assertEqual(keys, ['brands/wyld-test/deal-banner-missouri.png',
                                'brands/wyld-test/deal-banner-arizona.png'])
        self.assertEqual(mo.banner_url, CDN + 'brands/wyld-test/deal-banner-missouri.png')
        self.assertEqual(az.region_slug, 'arizona')
        self.assertEqual(self.brand.market_banner_ids, mo | az)

    def test_brand_level_arizona_art_is_left_alone(self):
        with patch(UPLOAD, side_effect=fake_upload):
            self.brand.write({'deal_banner': PNG})
            az_url = self.brand.deal_banner_url
            self.Banner.create({'brand_id': self.brand.id, 'region_id': self.mo.id, 'banner': PNG})
        self.assertEqual(az_url, CDN + 'brands/wyld-test/deal-banner.png')
        self.assertEqual(self.brand.deal_banner_url, az_url)

    def test_one_banner_per_brand_per_market(self):
        with patch(UPLOAD, side_effect=fake_upload):
            self.Banner.create({'brand_id': self.brand.id, 'region_id': self.mo.id})
            with self.assertRaises(Exception), mute_logger('odoo.sql_db'), self.env.cr.savepoint():
                self.Banner.create({'brand_id': self.brand.id, 'region_id': self.mo.id})
                self.env.flush_all()

    def test_clearing_the_banner_stops_serving_it(self):
        with patch(UPLOAD, side_effect=fake_upload):
            row = self.Banner.create({'brand_id': self.brand.id, 'region_id': self.mo.id, 'banner': PNG})
            self.assertTrue(row.banner_url)
            row.write({'banner': False})
        self.assertFalse(row.banner_url)

    def test_moving_a_banner_to_another_market_resyncs_its_key(self):
        with patch(UPLOAD, side_effect=fake_upload):
            row = self.Banner.create({'brand_id': self.brand.id, 'region_id': self.mo.id, 'banner': PNG})
            row.write({'region_id': self.az.id})
        self.assertEqual(row.banner_url, CDN + 'brands/wyld-test/deal-banner-arizona.png')

    def test_cdn_failure_still_saves_the_row(self):
        with patch(UPLOAD, side_effect=RuntimeError('R2 down')), mute_logger(
                'odoo.addons.mint_api_v2.models.mint_brand_market_banner'):
            row = self.Banner.create({'brand_id': self.brand.id, 'region_id': self.mo.id, 'banner': PNG})
        self.assertTrue(row.banner)
        self.assertFalse(row.banner_url)  # picture + no URL = the visible failure signal

    def test_deleting_the_brand_removes_its_market_banners(self):
        with patch(UPLOAD, side_effect=fake_upload):
            row = self.Banner.create({'brand_id': self.brand.id, 'region_id': self.mo.id, 'banner': PNG})
        self.brand.unlink()
        self.assertFalse(row.exists())
