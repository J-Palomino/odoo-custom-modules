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
    image = fields.Binary(string='Image', attachment=True)
    image_url = fields.Char(string='Image URL', help='External image URL (takes precedence over binary image)')
    product_image = fields.Binary(string='Product Image', attachment=True,
                                  help='Product photo displayed alongside the banner (best with Large size)')
    product_image_url = fields.Char(string='Product Image URL',
                                    help='External product image URL (takes precedence over uploaded product image)')
    title = fields.Char(string='Title')
    subtitle = fields.Text(string='Subtitle')
    link_url = fields.Char(string='Link URL')
    link_label = fields.Char(string='Link Label')
    background_color = fields.Char(string='Background Color', help='CSS color value (e.g. #e8f5ee)')
    sequence = fields.Integer(string='Sequence', default=10)
    active = fields.Boolean(string='Active', default=True)
    date_start = fields.Date(string='Start Date')
    date_end = fields.Date(string='End Date')
    company_id = fields.Many2one('res.company', string='Company', default=lambda self: self.env.company)

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
