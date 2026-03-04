# -*- coding: utf-8 -*-
from odoo import models, fields, api


class PushSubscription(models.Model):
    _name = 'mint.push.subscription'
    _description = 'Web Push Subscription'
    _order = 'create_date desc'
    _rec_name = 'endpoint'

    endpoint = fields.Char(required=True, index=True)
    p256dh = fields.Char('Public Key', required=True)
    auth = fields.Char('Auth Secret', required=True)
    site_id = fields.Many2one('mint.push.site', string='Site', ondelete='set null', index=True)
    user_agent = fields.Char('User Agent')
    is_active = fields.Boolean(default=True, index=True)
    last_sent = fields.Datetime('Last Notification Sent', readonly=True)

    _sql_constraints = [
        ('endpoint_unique', 'unique(endpoint)', 'This push endpoint is already registered.'),
    ]

    def _send_push(self, payload):
        """Send a web push notification to this subscription.

        Args:
            payload: dict with keys title, body, url, icon, image, actions
        Returns:
            True on success, False on failure
        """
        import json
        try:
            from pywebpush import webpush, WebPushException
        except ImportError:
            from odoo.exceptions import UserError
            raise UserError(
                'pywebpush is not installed. Run: pip install pywebpush'
            )

        ICP = self.env['ir.config_parameter'].sudo()
        vapid_private = ICP.get_param('mint.vapid_private_key')
        vapid_email = ICP.get_param('mint.vapid_email', 'mailto:info@mintdeals.com')

        if not vapid_private:
            return False

        subscription_info = {
            'endpoint': self.endpoint,
            'keys': {
                'p256dh': self.p256dh,
                'auth': self.auth,
            },
        }

        try:
            webpush(
                subscription_info=subscription_info,
                data=json.dumps(payload),
                vapid_private_key=vapid_private,
                vapid_claims={'sub': vapid_email},
                timeout=10,
            )
            self.sudo().write({'last_sent': fields.Datetime.now()})
            return True
        except WebPushException as e:
            # 410 Gone = subscription expired, deactivate it
            if hasattr(e, 'response') and e.response is not None and e.response.status_code in (404, 410):
                self.sudo().write({'is_active': False})
            return False
        except Exception:
            return False
