# -*- coding: utf-8 -*-
import json
import logging
import re
import urllib.request

from odoo import models, fields, api
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)

FRONTEND_URL_PARAM = 'mint_banner.frontend_url'
FRONTEND_URL_DEFAULT = 'https://mintdeals2026.pages.dev'
FRONTEND_API_KEY_PARAM = 'mint_banner.frontend_api_key'

# Allowed domains for external image URLs
ALLOWED_IMAGE_DOMAINS = {
    'letsgomint.us',
    'mintdeals.com',
    'res.cloudinary.com',
    'images.dutchie.com',
    'images.weedmaps.com',
    'd2l32dtrfxrh1d.cloudfront.net',
}


class MintBanner(models.Model):
    _name = 'mint.banner'
    _description = 'MintDeals Category Banner'
    _order = 'sequence, id'

    name = fields.Char(string='Name', required=True)
    slot = fields.Selection([
        ('hero', 'Hero (Top Carousel)'),
        ('after-flower', 'After Flower'),
        ('after-pre-rolls', 'After Pre-Rolls'),
        ('after-vapes', 'After Vapes'),
        ('after-edibles', 'After Edibles'),
        ('after-concentrates', 'After Concentrates'),
        ('brand-spotlight', 'Brand Spotlight (Below Hero)'),
        ('deals-popup', 'Deals Popup (Bottom-Right)'),
    ], string='Slot Type', required=True, default='hero',
       help='hero: top carousel on store pages. '
            'after-*: between product category sections. '
            'brand-spotlight: below hero, above deals. '
            'deals-popup: floating bottom-right popup (site-wide).')
    category = fields.Char(string='Category', help='Category cname (e.g. flower, edibles). Leave empty for default/catch-all.')
    brand = fields.Char(string='Brand', help='Brand name for click-through filter')
    search_term = fields.Char(string='Search Term', help='Product search term for click-through filter')
    size = fields.Selection([
        ('small', 'Small (Compact Strip)'),
        ('medium', 'Medium (Standard)'),
        ('large', 'Large (Featured)'),
    ], string='Size', default='medium', required=True,
       help='Small: compact horizontal strip. Medium: standard banner. Large: prominent callout with product photo.')
    image = fields.Image(string='Background Image', max_width=2048, max_height=1024,
                         attachment=True,
                         help='Aspect ratio is what fits the slot — sharpness comes from pixels. '
                              'Hero: 3.2:1 (e.g. 1280×400, 1920×600, 2560×800). '
                              'Category/Spotlight: 4:1 (e.g. 1200×300, 1600×400). '
                              'Deals Popup: 4:3 (e.g. 800×600). '
                              'Min 1280px wide for hero sharpness; max 2048×1024. JPG, PNG, or WebP.')
    image_url = fields.Char(string='Image URL',
                            help='External image URL (takes precedence over uploaded image). Must be HTTPS from an allowed domain.')
    product_image = fields.Image(string='Product Image', max_width=512, max_height=512,
                                 attachment=True,
                                 help='384×384px square PNG with transparent background. Max 512×512. Rendered 40-176px depending on size.')
    product_image_url = fields.Char(string='Product Image URL',
                                    help='External product image URL (takes precedence). Must be HTTPS. 384×384px square PNG recommended.')
    title = fields.Char(string='Title')
    subtitle = fields.Text(string='Subtitle')
    link_url = fields.Char(string='Link URL')
    link_label = fields.Char(string='Link Label')
    background_color = fields.Char(string='Background Color', help='CSS color value (e.g. #e8f5ee)')
    regions = fields.Char(string='Regions',
                          help='Comma-separated region slugs (e.g. arizona,michigan). '
                               'Leave empty to show on ALL regions.')
    store_slugs = fields.Char(string='Store Slugs',
                              help='Comma-separated store slugs (e.g. tempe,mesa). '
                                   'Leave empty to show on ALL stores.')
    sequence = fields.Integer(string='Sequence', default=10)
    active = fields.Boolean(string='Active', default=True)
    date_start = fields.Date(string='Start Date')
    date_end = fields.Date(string='End Date')
    company_id = fields.Many2one('res.company', string='Company', default=lambda self: self.env.company)

    @api.model_create_multi
    def create(self, vals_list):
        # When created against a child store company (Visual CMS upload, "Open in
        # Odoo" form, etc.) and Store Slugs is left blank, derive it from the
        # company's x_slug. The parent "Mint Cannabis" company has no slug, so
        # site-wide banners (deals-popup, brand-spotlight) stay global.
        Company = self.env['res.company'].sudo()
        for vals in vals_list:
            if (vals.get('store_slugs') or '').strip():
                continue
            company_id = vals.get('company_id')
            if not company_id:
                continue
            slug = (Company.browse(company_id).x_slug or '').strip()
            if slug:
                vals['store_slugs'] = slug
        return super().create(vals_list)

    @api.constrains('image_url', 'product_image_url')
    def _check_image_urls(self):
        """Validate that external image URLs use HTTPS and come from allowed domains."""
        for rec in self:
            for field_name, label in [('image_url', 'Image URL'), ('product_image_url', 'Product Image URL')]:
                url = rec[field_name]
                if not url:
                    continue
                url = url.strip()
                if not url.startswith('https://'):
                    raise ValidationError(f'{label} must use HTTPS. Got: {url}')
                # Extract domain
                match = re.match(r'https://([^/]+)', url)
                if not match:
                    raise ValidationError(f'{label} is not a valid URL: {url}')
                domain = match.group(1).lower()
                # Check against allowed domains (match domain or subdomain)
                if not any(domain == d or domain.endswith('.' + d) for d in ALLOWED_IMAGE_DOMAINS):
                    allowed = ', '.join(sorted(ALLOWED_IMAGE_DOMAINS))
                    raise ValidationError(
                        f'{label} domain "{domain}" is not allowed.\n'
                        f'Allowed domains: {allowed}\n'
                        f'Upload the image directly instead, or contact engineering to whitelist a new domain.'
                    )

    @api.constrains('link_url')
    def _check_link_url(self):
        """Validate link URLs are relative paths or HTTPS."""
        for rec in self:
            url = rec.link_url
            if not url:
                continue
            url = url.strip()
            if url.startswith('/'):
                continue  # Relative paths are fine
            if not url.startswith('https://'):
                raise ValidationError(
                    f'Link URL must be a relative path (starting with /) or an HTTPS URL. Got: {url}'
                )

    publish_status = fields.Selection(
        selection=[
            ('published', 'Published'),
            ('scheduled', 'Scheduled'),
            ('expired', 'Expired'),
            ('draft', 'Draft'),
        ],
        string='Status', compute='_compute_publish_status', store=False,
    )

    @api.depends('active', 'date_start', 'date_end')
    def _compute_publish_status(self):
        today = fields.Date.context_today(self)
        for rec in self:
            if not rec.active:
                rec.publish_status = 'draft'
            elif rec.date_end and rec.date_end < today:
                rec.publish_status = 'expired'
            elif rec.date_start and rec.date_start > today:
                rec.publish_status = 'scheduled'
            else:
                rec.publish_status = 'published'

    def action_clear_frontend_cache(self):
        """Call the frontend cache-clear endpoint so banner changes appear immediately."""
        icp = self.env['ir.config_parameter'].sudo()
        base_url = (icp.get_param(FRONTEND_URL_PARAM) or FRONTEND_URL_DEFAULT).rstrip('/')
        api_key = icp.get_param(FRONTEND_API_KEY_PARAM) or ''
        url = f'{base_url}/api/admin/clear-cache'
        payload = json.dumps({'pattern': 'banners'}).encode()

        if not api_key:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Configuration Missing',
                    'message': f'Set System Parameter "{FRONTEND_API_KEY_PARAM}" to the frontend ADMIN_API_KEY.',
                    'type': 'warning',
                    'sticky': True,
                },
            }

        try:
            req = urllib.request.Request(
                url, data=payload, method='POST',
                headers={
                    'Content-Type': 'application/json',
                    'Authorization': f'Bearer {api_key}',
                },
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = json.loads(resp.read())
                _logger.info('Frontend banner cache cleared: %s', body)
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Cache Cleared',
                    'message': 'Frontend banner cache has been cleared. Changes will appear on next page load.',
                    'type': 'success',
                    'sticky': False,
                },
            }
        except Exception as e:
            _logger.warning('Failed to clear frontend cache at %s: %s', url, e)
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Cache Clear Failed',
                    'message': f'Could not reach frontend: {e}',
                    'type': 'warning',
                    'sticky': False,
                },
            }
