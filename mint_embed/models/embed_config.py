# -*- coding: utf-8 -*-
"""
Embed configuration — each record represents a widget preset
that staff can generate and paste into WordPress.
"""
import uuid

from odoo import api, fields, models


class MintEmbedConfig(models.Model):
    _name = 'mint.embed.config'
    _description = 'Embed Widget Configuration'
    _order = 'create_date desc'
    embed_key_unique = models.Constraint(
        'UNIQUE(embed_key)',
        'Embed key must be unique.',
    )

    name = fields.Char(string='Label', required=True,
                       help='Internal name, e.g. "Homepage banner carousel"')
    embed_key = fields.Char(string='Embed Key', readonly=True, copy=False,
                            default=lambda self: uuid.uuid4().hex[:12],
                            help='Unique key used in the script tag')
    widget_type = fields.Selection([
        ('banners', 'Banners'),
        ('blog', 'Blog Feed'),
        ('form_contact', 'Contact Form'),
        ('form_newsletter', 'Newsletter Signup'),
        ('page', 'Info Page'),
    ], string='Widget Type', required=True, default='banners')

    # Banner options
    banner_slot = fields.Selection([
        ('hero', 'Hero (Top Carousel)'),
        ('after-flower', 'After Flower'),
        ('after-vapes', 'After Vapes'),
        ('after-edibles', 'After Edibles'),
        ('after-concentrates', 'After Concentrates'),
        ('brand-spotlight', 'Brand Spotlight'),
    ], string='Banner Slot')
    banner_company_id = fields.Many2one('res.company', string='Store (for banners)')

    # Blog options
    blog_limit = fields.Integer(string='Posts to show', default=3)
    blog_featured_only = fields.Boolean(string='Featured only', default=False)

    # Page reference
    page_id = fields.Many2one('mint.embed.page', string='Info Page')

    # Style overrides
    theme = fields.Selection([
        ('light', 'Light'),
        ('dark', 'Dark'),
        ('auto', 'Auto (match host)'),
    ], string='Theme', default='light')
    accent_color = fields.Char(string='Accent Color', default='#00954c',
                               help='Hex color for buttons and links')
    max_width = fields.Char(string='Max Width', default='100%',
                            help='CSS max-width, e.g. 800px or 100%')

    active = fields.Boolean(default=True)
    embed_code = fields.Text(string='Embed Code', compute='_compute_embed_code')

    @api.depends('embed_key', 'widget_type', 'theme', 'accent_color', 'max_width',
                 'banner_slot', 'banner_company_id', 'blog_limit', 'blog_featured_only',
                 'page_id')
    def _compute_embed_code(self):
        base_url = self.env['ir.config_parameter'].sudo().get_param(
            'web.base.url', 'https://letsgomint.us')
        for rec in self:
            attrs = [
                f'src="{base_url}/embed/mint-widget.js"',
                f'data-key="{rec.embed_key}"',
                f'data-type="{rec.widget_type}"',
                f'data-base="{base_url}"',
            ]
            if rec.theme and rec.theme != 'light':
                attrs.append(f'data-theme="{rec.theme}"')
            if rec.accent_color and rec.accent_color != '#00954c':
                attrs.append(f'data-accent="{rec.accent_color}"')
            if rec.max_width and rec.max_width != '100%':
                attrs.append(f'data-max-width="{rec.max_width}"')
            # Type-specific attrs
            if rec.widget_type == 'banners':
                if rec.banner_slot:
                    attrs.append(f'data-slot="{rec.banner_slot}"')
                if rec.banner_company_id:
                    attrs.append(f'data-store="{rec.banner_company_id.id}"')
            elif rec.widget_type == 'blog':
                if rec.blog_limit and rec.blog_limit != 3:
                    attrs.append(f'data-limit="{rec.blog_limit}"')
                if rec.blog_featured_only:
                    attrs.append('data-featured="true"')
            elif rec.widget_type == 'page' and rec.page_id:
                attrs.append(f'data-page="{rec.page_id.slug}"')

            tag = '<script ' + ' '.join(attrs) + '></script>'
            rec.embed_code = tag
