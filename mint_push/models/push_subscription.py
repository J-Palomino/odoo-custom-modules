# -*- coding: utf-8 -*-
import json
import logging
import time
import urllib.request

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

# Module-level cache for FCM access token (shared across requests within a worker)
_fcm_token_cache = {'token': None, 'expires_at': 0}


class PushSubscription(models.Model):
    _name = 'mint.push.subscription'
    _description = 'Browser Push Subscription'
    _order = 'created_at desc'

    endpoint = fields.Text(string='Endpoint', required=True, index=True)
    key_p256dh = fields.Text(string='P256DH Key')
    key_auth = fields.Text(string='Auth Secret')
    site_id = fields.Many2one('mint.push.site', string='Site', ondelete='set null', index=True)
    partner_id = fields.Many2one('res.partner', string='Partner', ondelete='set null')
    created_at = fields.Datetime(string='Created At', default=fields.Datetime.now)
    fail_count = fields.Integer(string='Consecutive Failures', default=0)

    # Native app support (Capacitor iOS/Android)
    platform = fields.Selection([
        ('web', 'Web Push'),
        ('ios', 'iOS (APNs)'),
        ('android', 'Android (FCM)'),
    ], string='Platform', default='web', required=True, index=True)
    device_token = fields.Char(string='Device Token',
        help='FCM/APNs device token for native apps. Empty for web push.')

    # Constraint managed at DB level — endpoint already has UNIQUE index
    # Removed models.Constraint to avoid Odoo 19 __set_name__ issues

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

    def _send_push(self, subscription_info, payload, platform='web'):
        """Send a single push notification.

        Returns:
            True — success
            'gone' — subscription expired (should delete)
            'skip' — non-web platform, no sender available yet
            False — transient failure
        """
        # Native platforms need FCM/APNs — skip if not implemented yet
        if platform in ('ios', 'android'):
            sent = self._send_fcm(subscription_info, payload)
            if sent is None:
                return 'skip'
            return sent

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
            if hasattr(e, 'response') and e.response is not None:
                if e.response.status_code in (404, 410):
                    return 'gone'
            return False
        except Exception as e:
            _logger.exception("Unexpected push error: %s", e)
            return False

    # ==================== FCM Delivery ====================

    def _get_fcm_config(self):
        """Read Firebase service account JSON from system parameters.

        Returns (project_id, service_account_dict) or (None, None) if not configured.
        """
        ICP = self.env['ir.config_parameter'].sudo()
        sa_json = ICP.get_param('mint.fcm_service_account', '')
        if not sa_json:
            return None, None
        try:
            sa = json.loads(sa_json)
            project_id = sa.get('project_id')
            if not project_id or not sa.get('private_key'):
                _logger.error("FCM service account missing project_id or private_key")
                return None, None
            return project_id, sa
        except (json.JSONDecodeError, TypeError) as e:
            _logger.error("FCM service account JSON invalid: %s", e)
            return None, None

    def _get_fcm_access_token(self, service_account):
        """Generate an OAuth2 access token for FCM using the service account.

        Signs a JWT with the service account's private key and exchanges it
        for a short-lived access token. Tokens are cached for 50 minutes
        (they expire at 60 minutes).

        Uses only `cryptography` (already a dependency of py_vapid) — no
        additional pip packages needed.
        """
        global _fcm_token_cache

        # Return cached token if still valid (50-min TTL with 10-min buffer)
        if _fcm_token_cache['token'] and time.time() < _fcm_token_cache['expires_at']:
            return _fcm_token_cache['token']

        try:
            import base64
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
            from cryptography.hazmat.backends import default_backend

            # Build JWT header + claims
            now = int(time.time())
            header = base64.urlsafe_b64encode(json.dumps(
                {"alg": "RS256", "typ": "JWT"}).encode()).rstrip(b'=')
            claims = base64.urlsafe_b64encode(json.dumps({
                "iss": service_account['client_email'],
                "scope": "https://www.googleapis.com/auth/firebase.messaging",
                "aud": "https://oauth2.googleapis.com/token",
                "iat": now,
                "exp": now + 3600,
            }).encode()).rstrip(b'=')

            # Sign with RSA private key
            signing_input = header + b'.' + claims
            private_key = serialization.load_pem_private_key(
                service_account['private_key'].encode(),
                password=None,
                backend=default_backend(),
            )
            signature = private_key.sign(
                signing_input,
                asym_padding.PKCS1v15(),
                hashes.SHA256(),
            )
            sig_b64 = base64.urlsafe_b64encode(signature).rstrip(b'=')
            jwt_token = (signing_input + b'.' + sig_b64).decode('ascii')

            # Exchange JWT for access token
            token_data = json.dumps({
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": jwt_token,
            }).encode()
            req = urllib.request.Request(
                "https://oauth2.googleapis.com/token",
                data=token_data,
                headers={"Content-Type": "application/json"},
            )
            resp = urllib.request.urlopen(req, timeout=10)
            result = json.loads(resp.read())
            access_token = result['access_token']

            # Cache with 50-min TTL
            _fcm_token_cache['token'] = access_token
            _fcm_token_cache['expires_at'] = time.time() + 3000

            return access_token

        except Exception as e:
            _logger.error("Failed to get FCM access token: %s", e)
            _fcm_token_cache['token'] = None
            _fcm_token_cache['expires_at'] = 0
            return None

    def _send_fcm(self, subscription_info, payload):
        """Send a notification via FCM HTTP v1.

        Returns True on success, 'gone' if token is invalid, False on
        transient failure, or None if FCM is not configured.
        """
        project_id, service_account = self._get_fcm_config()
        if not project_id:
            return None  # Not configured — caller will skip

        # Extract device token: prefer device_token field, fall back to endpoint
        endpoint = subscription_info.get('endpoint', '')
        token = subscription_info.get('device_token', '')
        if not token:
            # Parse synthetic endpoint like "ios://abc123" or "android://abc123"
            if '://' in endpoint:
                token = endpoint.split('://', 1)[1]
            else:
                token = endpoint
        if not token:
            _logger.warning("FCM send: no device token for endpoint %s", endpoint[:60])
            return 'gone'

        access_token = self._get_fcm_access_token(service_account)
        if not access_token:
            return False  # Transient — token generation failed

        # Build FCM v1 message
        fcm_message = {
            "message": {
                "token": token,
                "notification": {
                    "title": payload.get('title', 'Mint Cannabis'),
                    "body": payload.get('body', ''),
                },
                "data": {
                    "url": payload.get('url', 'https://mintdeals2026.pages.dev'),
                    "icon": payload.get('icon', '/favicon.png'),
                },
            }
        }

        # Add image if present
        if payload.get('image'):
            fcm_message['message']['notification']['image'] = payload['image']

        # Add action buttons as data payload (handled by client app)
        if payload.get('actions'):
            fcm_message['message']['data']['actions'] = json.dumps(payload['actions'])

        # Android-specific: high priority for deal alerts
        fcm_message['message']['android'] = {
            "priority": "high",
            "notification": {
                "click_action": "OPEN_URL",
                "channel_id": "mint_deals",
            }
        }

        # APNs-specific: badge + sound
        fcm_message['message']['apns'] = {
            "payload": {
                "aps": {
                    "badge": 1,
                    "sound": "default",
                    "mutable-content": 1,
                }
            }
        }

        try:
            url = "https://fcm.googleapis.com/v1/projects/%s/messages:send" % project_id
            body = json.dumps(fcm_message).encode()
            req = urllib.request.Request(url, data=body, headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer %s" % access_token,
            })
            resp = urllib.request.urlopen(req, timeout=10)
            _logger.info("FCM sent to %s...%s", token[:8], token[-4:] if len(token) > 12 else '')
            return True

        except urllib.error.HTTPError as e:
            status = e.code
            try:
                error_body = json.loads(e.read())
                error_detail = error_body.get('error', {}).get('message', str(e))
            except Exception:
                error_detail = str(e)

            if status == 404 or status == 400:
                # Invalid token or unregistered device
                if 'UNREGISTERED' in str(error_detail).upper() or status == 404:
                    _logger.info("FCM token expired/unregistered: %s", token[:20])
                    return 'gone'
            elif status == 401:
                # Access token expired — clear cache so next call regenerates
                _fcm_token_cache['token'] = None
                _fcm_token_cache['expires_at'] = 0
                _logger.warning("FCM auth failed (401), cleared token cache")

            _logger.warning("FCM send failed (%d) for %s: %s",
                            status, token[:20], error_detail)
            return False

        except Exception as e:
            _logger.exception("FCM unexpected error for %s: %s", token[:20], e)
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

        skipped = 0
        for sub in subscriptions:
            sub_info = {
                'endpoint': sub.endpoint,
                'keys': {
                    'p256dh': sub.key_p256dh,
                    'auth': sub.key_auth,
                },
                'device_token': sub.device_token or '',
            }
            result = self._send_push(sub_info, payload, platform=sub.platform or 'web')
            if result is True:
                if sub.fail_count > 0:
                    to_reset |= sub
            elif result == 'skip':
                skipped += 1
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

        sent = len(subscriptions) - len(to_delete) - skipped
        _logger.info("Push sent to %d/%d subscriptions (%d skipped native)",
                      sent, len(subscriptions), skipped)
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

        # Say so when there is nothing to send to.
        #
        # A customer whose duplicate partner was archived keeps her
        # subscriptions pointing at the archived row, so a send addressed to the
        # SURVIVING partner matches nothing and returns 0 — no error, no log,
        # the notification simply never arrives. That is how this went unnoticed
        # until someone tested a redemption by hand (Koreena, partner 78175).
        # Logging is cheap and it is the only signal this failure mode produces.
        if not subscriptions:
            _logger.info(
                'push: no subscriptions for partner %s; %s subscription(s) '
                'system-wide point at an archived partner',
                partner_id, len(self.find_orphaned_subscriptions()))

        sent = 0
        for sub in subscriptions:
            sub_info = {
                'endpoint': sub.endpoint,
                'keys': {
                    'p256dh': sub.key_p256dh,
                    'auth': sub.key_auth,
                },
                'device_token': sub.device_token or '',
            }
            result = self._send_push(sub_info, payload, platform=sub.platform or 'web')
            if result is True:
                sent += 1
            elif result == 'gone':
                sub.sudo().unlink()

        return sent

    def find_orphaned_subscriptions(self):
        """Subscriptions whose partner has been archived.

        These are silently unreachable: during a merge the login is repointed to
        the surviving partner, but partner_id here is left behind, so
        send_to_partner(surviving_id) finds nothing and returns 0.

        Exposed rather than inlined because the reconciliation cron in
        Identity-Link T2 (#109605) is the right place to repair these — at the
        moment of the merge it knows both sides, which this cannot.
        """
        subs = self.sudo().search([('partner_id', '!=', False)])
        if not subs:
            return subs
        partners = subs.mapped('partner_id').with_context(active_test=False)
        archived = set(partners.filtered(lambda p: not p.active).ids)
        return subs.filtered(lambda s: s.partner_id.id in archived)

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

        skipped = 0
        for sub in subscriptions:
            sub_info = {
                'endpoint': sub.endpoint,
                'keys': {
                    'p256dh': sub.key_p256dh,
                    'auth': sub.key_auth,
                },
                'device_token': sub.device_token or '',
            }
            result = self._send_push(sub_info, payload, platform=sub.platform or 'web')
            if result is True:
                if sub.fail_count > 0:
                    to_reset |= sub
            elif result == 'skip':
                skipped += 1
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

        sent = len(subscriptions) - len(to_delete) - skipped
        _logger.info("Proximity push sent to %d/%d subscriptions within %s miles (%d skipped native)",
                      sent, len(subscriptions), radius_miles, skipped)
        return sent
