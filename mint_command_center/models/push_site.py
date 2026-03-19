# -*- coding: utf-8 -*-
from odoo import models, fields


class PushSite(models.Model):
    _name = 'mint.push.site'
    _description = 'Push Notification Site'
    _order = 'sequence, name'

    name = fields.Char(string='Site Name', required=True,
                       help='Display name (e.g. "MintDeals PWA", "Mint Mobile App")')
    code = fields.Char(string='Site Code', required=True, index=True,
                       help='Unique identifier sent by the frontend (e.g. "mintdeals")')
    url = fields.Char(string='Site URL',
                      help='Base URL of the frontend (e.g. "https://letsgomint.us")')
    active = fields.Boolean(default=True)
    sequence = fields.Integer(default=10)
    subscription_ids = fields.One2many('mint.push.subscription', 'site_id',
                                       string='Subscriptions')
    subscription_count = fields.Integer(string='Subscribers',
                                        compute='_compute_subscription_count')

    _code_unique = models.Constraint(
        'UNIQUE(code)',
        'Site code must be unique.',
    )

    def _compute_subscription_count(self):
        for site in self:
            site.subscription_count = len(site.subscription_ids)
