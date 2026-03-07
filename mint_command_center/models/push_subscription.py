# -*- coding: utf-8 -*-
from odoo import models, fields


class PushSubscriptionExt(models.Model):
    """Extend mint_push subscription with fields used by Command Center views."""
    _inherit = 'mint.push.subscription'

    user_agent = fields.Char('User Agent')
    is_active = fields.Boolean(default=True, index=True)
    last_sent = fields.Datetime('Last Notification Sent', readonly=True)

    # Store-based segmentation
    store_id = fields.Many2one(
        'res.company', string='Selected Store',
        ondelete='set null', index=True,
        help='Store the subscriber selected when they subscribed')
    region = fields.Char('Region', index=True,
        help='State/region slug (e.g., "arizona")')

    # GPS proximity targeting
    latitude = fields.Float('Latitude', digits=(10, 7))
    longitude = fields.Float('Longitude', digits=(10, 7))
    geo_updated_at = fields.Datetime('Location Updated')
