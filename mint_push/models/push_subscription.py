# -*- coding: utf-8 -*-
import json
import logging

from odoo import models, fields, api

_logger = logging.getLogger(__name__)

try:
    from pywebpush import webpush, WebPushException
except ImportError:
    webpush = None
    _logger.warning("pywebpush not installed — push notifications disabled")

try:
    from py_vapid import Vapid
except ImportError:
    Vapid = None
    _logger.warning("py_vapid not installed — VAPID key auto-generation disabled")


class PushSubscription(models.Model):
    _name = 'mint.push.subscription'
    _description = 'Browser Push Subscription'
    _order = 'created_at desc'

    endpoint = fields.Text(string='Endpoint', required=True, index=True)
    key_p256dh = fields.Text(string='P256DH Key', required=True)
    key_auth = fields.Text(string='Auth Secret', required=True)
    site_id = fields.Many2one('mint.push.site', string='Site', ondelete='set null', index=True)
    partner_id = fields.Many2one('res.partner', string='Partner', ondelete='set null')
    created_at = fields.Datetime(string='Created At', default=fields.Datetime.now)
    fail_count = fields.Integer(string='Consecutive Failures', default=0)

    _sql_constraints = [
        ('endpoint_unique', 'UNIQUE(endpoint)', 'Subscription endpoint must be unique.'),
    ]

    def _ensure_vapid_keys(self):
        """Ensure VAPID keys exist, generating them on first use.

        Returns the public key string.
        """
        ICP = self.env['ir.config_parameter'].sudo()
        public_key = ICP.get_param('mint.vapid_public_key', '')
        if public_key:
            return public_key

        if not Vapid:
            _logger.error("py_vapid not available — cannot generate VAPID keys")
            return ''

        _logger.info("Generating new VAPID keypair...")
        vapid = Vapid()
        vapid.generate_keys()

        # Export raw base64url-encoded keys
        raw_priv = vapid.private_pem()
        raw_pub = vapid.public_key

        # vapid.public_key is a CryptoKey; encode to the uncompressed point
        import base64
        from cryptography.hazmat.primitives.serialization import (
            Encoding, PublicFormat,
        )
        pub_bytes = raw_pub.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
        pub_b64 = base64.urlsafe_b64encode(pub_bytes).rstrip(b'=').decode('ascii')

        # Store PEM private key and base64url public key
        priv_pem = raw_priv.decode('utf-8') if isinstance(raw_priv, bytes) else raw_priv
        ICP.set_param('mint.vapid_private_key', priv_pem)
        ICP.set_param('mint.vapid_public_key', pub_b64)
        _logger.info("VAPID keys generated and stored (public: %s...)", pub_b64[:20])
        return pub_b64

    def _get_vapid_keys(self):
        """Read VAPID keys from system parameters, auto-generating if needed."""
        self._ensure_vapid_keys()
        ICP = self.env['ir.config_parameter'].sudo()
        private_key = ICP.get_param('mint.vapid_private_key', '')
        public_key = ICP.get_param('mint.vapid_public_key', '')
        return private_key, public_key

    def _send_push(self, subscription_info, payload):
        """Send a single push notification. Returns True on success."""
        if not webpush:
            _logger.error("pywebpush not available")
            return False

        private_key, public_key = self._get_vapid_keys()
        if not private_key or not public_key:
            _logger.error("VAPID keys not configured")
            return False

        try:
            webpush(
                subscription_info=subscription_info,
                data=json.dumps(payload),
                vapid_private_key=private_key,
                vapid_claims={"sub": "mailto:admin@letsgomint.us"},
            )
            return True
        except WebPushException as e:
            _logger.warning("Push failed for %s: %s", subscription_info.get('endpoint', '?'), e)
            # 410 Gone or 404 means subscription is invalid
            if hasattr(e, 'response') and e.response is not None:
                if e.response.status_code in (404, 410):
                    return 'gone'
            return False
        except Exception as e:
            _logger.exception("Unexpected push error: %s", e)
            return False

    def _build_payload(self, title, body, url=None, icon=None, image=None, actions=None):
        """Build the push notification JSON payload."""
        payload = {
            'title': title,
            'body': body,
            'url': url or 'https://letsgomint.us',
            'icon': icon or '/favicon.png',
        }
        if image:
            payload['image'] = image
        if actions:
            # actions: [{"action": "shop", "title": "Shop Now", "url": "/deals"}]
            payload['actions'] = actions
        return payload

    def send_to_all(self, title, body, url=None, icon=None, image=None,
                    actions=None, site_id=None):
        """Send a notification to all active subscriptions.

        Args:
            site_id: Optional mint.push.site ID to filter subscribers by site.
        """
        payload = self._build_payload(title, body, url=url, icon=icon,
                                      image=image, actions=actions)

        domain = [('site_id', '=', site_id)] if site_id else []
        subscriptions = self.sudo().search(domain)
        to_delete = self.env['mint.push.subscription']
        to_reset = self.env['mint.push.subscription']

        for sub in subscriptions:
            sub_info = {
                'endpoint': sub.endpoint,
                'keys': {
                    'p256dh': sub.key_p256dh,
                    'auth': sub.key_auth,
                },
            }
            result = self._send_push(sub_info, payload)
            if result is True:
                if sub.fail_count > 0:
                    to_reset |= sub
            elif result == 'gone':
                to_delete |= sub
            else:
                new_count = sub.fail_count + 1
                if new_count >= 3:
                    to_delete |= sub
                else:
                    sub.sudo().write({'fail_count': new_count})

        if to_reset:
            to_reset.sudo().write({'fail_count': 0})
        if to_delete:
            _logger.info("Removing %d stale push subscriptions", len(to_delete))
            to_delete.sudo().unlink()

        sent = len(subscriptions) - len(to_delete)
        _logger.info("Push sent to %d/%d subscriptions", sent, len(subscriptions))
        return sent

    def send_to_partner(self, partner_id, title, body, url=None, icon=None,
                        image=None, actions=None, site_id=None):
        """Send a notification to a specific partner's subscriptions."""
        payload = self._build_payload(title, body, url=url, icon=icon,
                                      image=image, actions=actions)

        domain = [('partner_id', '=', partner_id)]
        if site_id:
            domain.append(('site_id', '=', site_id))
        subscriptions = self.sudo().search(domain)
        sent = 0
        for sub in subscriptions:
            sub_info = {
                'endpoint': sub.endpoint,
                'keys': {
                    'p256dh': sub.key_p256dh,
                    'auth': sub.key_auth,
                },
            }
            result = self._send_push(sub_info, payload)
            if result is True:
                sent += 1
            elif result == 'gone':
                sub.sudo().unlink()

        return sent
