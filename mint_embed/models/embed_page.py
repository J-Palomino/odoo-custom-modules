# -*- coding: utf-8 -*-
"""
Embeddable info pages — rich HTML content managed in Odoo,
rendered inside the embed widget on external sites.
"""
from odoo import fields, models


class MintEmbedPage(models.Model):
    _name = 'mint.embed.page'
    _description = 'Embeddable Info Page'
    _order = 'sequence, name'

    name = fields.Char(string='Title', required=True)
    slug = fields.Char(string='Slug', required=True, index=True,
                       help='URL-safe identifier, e.g. "app-info" or "loyalty-program"')
    content = fields.Html(string='Content', sanitize=False,
                          help='Rich HTML content rendered inside the embed widget')
    summary = fields.Text(string='Summary',
                          help='Short description shown above the content')
    hero_image_url = fields.Char(string='Hero Image URL')
    hero_image = fields.Binary(string='Hero Image', attachment=True)
    cta_label = fields.Char(string='CTA Button Label',
                            help='e.g. "Download the App"')
    cta_url = fields.Char(string='CTA Button URL',
                          help='e.g. app store link')
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
