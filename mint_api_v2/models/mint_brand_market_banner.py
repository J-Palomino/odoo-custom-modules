# -*- coding: utf-8 -*-
"""Deal banner art per brand per market.

`mint.brand.deal_banner` is ONE image per brand, and it holds the Arizona
"Abstract Background" set. Missouri's deal cards use a different style
("Heady Background", Drive: Brands - MO / MO Website Deal Cards, 2026-09-18),
and 17 of those 69 brands already carried AZ art -- a single field would have
overwritten it. So art is keyed by (brand, market) here; the storefront takes
the row for the page's market first, and the brand-level deal_banner stays the
Arizona art.
"""
import base64
import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class MintBrandMarketBanner(models.Model):
    _name = 'mint.brand.market.banner'
    _description = 'Brand Deal Banner per Market'
    _order = 'brand_id, region_id'

    brand_id = fields.Many2one(
        'mint.brand', string='Brand', required=True, ondelete='cascade', index=True,
    )
    region_id = fields.Many2one(
        'mint.region', string='Market', required=True, ondelete='cascade', index=True,
    )
    # Stored so the storefront can filter by the slug it already has
    # (`getRegionSlug`) without a second read of mint.region.
    region_slug = fields.Char(
        related='region_id.slug', store=True, string='Market Slug',
    )
    banner = fields.Binary(
        string='Deal Banner', attachment=True,
        help='1240x310 (exactly 4:1), no logo or wordmark -- the card draws '
             'the brand and offer on top. Uploading pushes it to the CDN and '
             'fills Banner URL, which is what the storefront reads.',
    )
    banner_url = fields.Char(
        string='Banner URL', readonly=True, copy=False,
        help='CDN copy of the banner; set automatically on upload.',
    )

    _brand_region_unique = models.Constraint(
        'unique(brand_id, region_id)',
        'A brand can have only one deal banner per market.',
    )

    def _sync_banner_to_r2(self):
        """Upload the banner to Cloudflare R2 and set banner_url.

        Keyed `brands/<brand>/deal-banner-<market>.<ext>` so each market's art
        is its own object -- the Arizona art at `deal-banner.<ext>` is never
        touched. Failures are logged, not raised, like the brand-level sync: a
        row with a banner and no URL is the visible sign the upload failed.
        """
        self.ensure_one()
        try:
            image_bytes = base64.b64decode(self.banner)
            content_type, ext = self.brand_id._detect_image_type(image_bytes)
            market = self.region_id.slug or 'market-%s' % self.region_id.id
            key = 'brands/%s/deal-banner-%s.%s' % (self.brand_id._cdn_slug(), market, ext)

            from ..utils.r2_upload import upload_to_r2
            url = upload_to_r2(image_bytes, key, content_type)
            super(MintBrandMarketBanner, self).write({'banner_url': url})
            _logger.info('Synced %s deal banner to R2 for %s: %s', market, self.brand_id.name, url)
        except Exception:
            _logger.exception('Failed to sync %s deal banner to R2 for %s',
                              self.region_id.name, self.brand_id.name)

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for record in records:
            if record.banner:
                record._sync_banner_to_r2()
        return records

    def write(self, vals):
        res = super().write(vals)
        if 'banner' in vals and not vals['banner']:
            # Cleared: stop the storefront serving the old CDN copy.
            super().write({'banner_url': False})
        elif {'banner', 'brand_id', 'region_id'} & set(vals):
            # New art, or it moved to another brand/market (the CDN key changes).
            for record in self:
                if record.banner:
                    record._sync_banner_to_r2()
        return res


class MintBrand(models.Model):
    _inherit = 'mint.brand'

    market_banner_ids = fields.One2many(
        'mint.brand.market.banner', 'brand_id', string='Market Banners',
    )
