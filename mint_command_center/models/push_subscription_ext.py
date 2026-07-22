# -*- coding: utf-8 -*-
"""Deliver the welcome free pre-roll the moment a customer enables push.

Onboarding (/welcome step 3 -> /notify) asks for notification permission; this
is the pay-off. When the browser's subscription is stored we push the customer
their free pre-roll instead of leaving them to go find it on /rewards.

Hooked on create() rather than in mint_push's /api/v1/push/subscribe controller
so mint_push stays unaware of coupons: the subscribe endpoint upserts, and only
the CREATE path is a genuinely new subscription. A re-subscribe of an existing
endpoint takes the write() path (the FE calls the same endpoint on every store
change via updatePushMetadata), so gating on create already avoids the obvious
re-send; welcome_push_sent then makes it idempotent even across a reinstall.
"""
import logging

from odoo import api, models

_logger = logging.getLogger(__name__)


class MintPushSubscription(models.Model):
    _inherit = 'mint.push.subscription'

    @api.model_create_multi
    def create(self, vals_list):
        subscriptions = super().create(vals_list)
        Discount = self.env['mint.discount'].sudo()
        for sub in subscriptions:
            partner = sub.partner_id
            if not partner:
                continue  # anonymous subscriber — nothing to attribute a coupon to
            # Best-effort: a push/config failure must never break the subscribe
            # itself, or the customer loses notifications AND the coupon.
            try:
                Discount._send_welcome_coupon_push(partner)
            except Exception as e:
                _logger.warning(
                    'welcome_preroll: push on subscribe failed for partner %s: %s',
                    partner.id, e)
        return subscriptions
