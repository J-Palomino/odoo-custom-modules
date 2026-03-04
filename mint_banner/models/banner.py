# -*- coding: utf-8 -*-
import logging

from odoo import models, fields

_logger = logging.getLogger(__name__)


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
    ], string='Slot', required=True, default='hero')
    category = fields.Char(string='Category', help='Category cname (e.g. flower, edibles). Leave empty for default/catch-all.')
    image = fields.Binary(string='Image', attachment=True)
    image_url = fields.Char(string='Image URL', help='External image URL (takes precedence over binary image)')
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
