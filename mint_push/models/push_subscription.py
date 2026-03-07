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
                    actions=None, site_id=None, company_ids=None, region=None):
        """Send a notification to all active subscriptions.

        Args:
            site_id: Optional mint.push.site ID to filter subscribers by site.
            company_ids: Optional list of res.company IDs to filter by store.
            region: Optional region slug to filter by region.
        """
        payload = self._build_payload(title, body, url=url, icon=icon,
                                      image=image, actions=actions)

        domain = [('site_id', '=', site_id)] if site_id else []
        if company_ids and 'store_id' in self._fields:
            domain.append(('store_id', 'in', company_ids))
        if region and 'region' in self._fields:
            domain.append(('region', '=', region))
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

    def send_to_nearby(self, lat, lng, radius_miles, title, body,
                       url=None, icon=None, image=None, actions=None):
        """Send a notification to subscribers within radius_miles of (lat, lng).

        Uses the Haversine formula to find nearby subscriptions with GPS data.
        Requires mint_command_center module (provides latitude/longitude fields).
        """
        if 'latitude' not in self._fields:
            _logger.warning("send_to_nearby requires mint_command_center module (latitude/longitude fields)")
            return 0

        self.env.cr.execute("""
            SELECT id FROM mint_push_subscription
            WHERE latitude IS NOT NULL AND longitude IS NOT NULL
              AND (3959 * acos(
                    cos(radians(%s)) * cos(radians(latitude))
                    * cos(radians(longitude) - radians(%s))
                    + sin(radians(%s)) * sin(radians(latitude))
              )) <= %s
        """, (lat, lng, lat, radius_miles))
        sub_ids = [row[0] for row in self.env.cr.fetchall()]

        if not sub_ids:
            _logger.info("No subscribers within %s miles of (%s, %s)", radius_miles, lat, lng)
            return 0

        subscriptions = self.sudo().browse(sub_ids)
        payload = self._build_payload(title, body, url=url, icon=icon,
                                      image=image, actions=actions)
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
        _logger.info("Proximity push sent to %d/%d subscriptions within %s miles",
                      sent, len(subscriptions), radius_miles)
        return sent
