# -*- coding: utf-8 -*-
import json
import logging
import urllib.request

from odoo import models, fields, api

_logger = logging.getLogger(__name__)

FRONTEND_URL_PARAM = 'mint_banner.frontend_url'
FRONTEND_URL_DEFAULT = 'https://mintdeals2026.pages.dev'


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
    ], string='Slot Type', required=True, default='hero')
    category = fields.Char(string='Category', help='Category cname (e.g. flower, edibles). Leave empty for default/catch-all.')
    brand = fields.Char(string='Brand', help='Brand name for click-through filter')
    search_term = fields.Char(string='Search Term', help='Product search term for click-through filter')
    size = fields.Selection([
        ('small', 'Small (Compact Strip)'),
        ('medium', 'Medium (Standard)'),
        ('large', 'Large (Featured)'),
    ], string='Size', default='medium', required=True,
       help='Small: compact horizontal strip. Medium: standard banner. Large: prominent callout with product photo.')
    image = fields.Binary(string='Image', attachment=True,
                          help='Hero: 1600×400px (4:1). Category/Spotlight: 1200×300px. Deals Popup: 800×600px.')
    image_url = fields.Char(string='Image URL',
                            help='External image URL (takes precedence over uploaded image). Same sizes apply.')
    product_image = fields.Binary(string='Product Image', attachment=True,
                                  help='384×384px square PNG with transparent background. Rendered 40-176px depending on size.')
    product_image_url = fields.Char(string='Product Image URL',
                                    help='External product image URL (takes precedence). 384×384px square PNG recommended.')
    title = fields.Char(string='Title')
    subtitle = fields.Text(string='Subtitle')
    link_url = fields.Char(string='Link URL')
    link_label = fields.Char(string='Link Label')
    background_color = fields.Char(string='Background Color', help='CSS color value (e.g. #e8f5ee)')
    sequence = fields.Integer(string='Sequence', default=10)
    active = fields.Boolean(string='Active', default=True)
    date_start = fields.Date(string='Start Date')
    date_end = fields.Date(string='End Date')
    x_regions = fields.Char(string='Regions', help='Comma-separated region slugs. Leave empty = show on all regions.')
    x_store_slugs = fields.Char(string='Store Slugs', help='Comma-separated store slugs. Leave empty = show on all stores in targeted regions.')
    company_id = fields.Many2one('res.company', string='Company', default=lambda self: self.env.company)

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
        url = f'{base_url}/api/admin/clear-cache'
        payload = json.dumps({'pattern': 'banners'}).encode()

        try:
            req = urllib.request.Request(
                url, data=payload, method='POST',
                headers={'Content-Type': 'application/json'},
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
