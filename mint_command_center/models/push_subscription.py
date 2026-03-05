# -*- coding: utf-8 -*-
from odoo import models, fields


class PushSubscriptionExt(models.Model):
    """Extend mint_push subscription with fields used by Command Center views."""
    _inherit = 'mint.push.subscription'

    user_agent = fields.Char('User Agent')
    is_active = fields.Boolean(default=True, index=True)
    last_sent = fields.Datetime('Last Notification Sent', readonly=True)
