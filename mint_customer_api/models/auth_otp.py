# -*- coding: utf-8 -*-
"""
Signup phone-verification one-time codes (mint.auth.otp).

Why phone and not email: production Odoo cannot deliver customer email
(no ir.mail_server; mint_mail_whitelist strips non-company recipients — see
task #110693), and the frontend Worker holds no email-provider credentials.
The only outbound channels that exist are the Telnyx SMS gateway
(mint_sms_telnyx, currently disabled via ICP) and the BlueBubbles iMessage
bridge (enabled). This model chains them: Telnyx first when enabled, then
BlueBubbles. The verification requirement itself is feature-flagged in
register() (`mint_customer_api.require_phone_verification`), so the flow can
ship dark and be turned on once a transport is confirmed healthy.

Consent posture: a verification code is a customer-initiated transactional
message (the customer just typed their number and asked for the code), not
marketing — it deliberately does NOT require the res.partner consent ledger,
which cannot exist yet pre-signup. No cannabis terminology in the body.
"""
import hashlib
import hmac
import json
import logging
import secrets

import requests

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

OTP_LENGTH = 6
OTP_TTL_MINUTES = 10
OTP_MAX_ATTEMPTS = 5
OTP_RESEND_COOLDOWN_SECONDS = 60
OTP_MAX_PER_PHONE_PER_HOUR = 5
OTP_RETENTION_HOURS = 24

OTP_BODY = (
    "Your Mint verification code is %s. It expires in "
    + str(OTP_TTL_MINUTES) + " minutes. If you didn't request this, ignore it."
)

SEND_TIMEOUT = 15  # seconds; runs inside an HTTP request, keep tight


class MintAuthOtp(models.Model):
    _name = 'mint.auth.otp'
    _description = 'Web Signup Phone Verification Code'
    _order = 'create_date desc'

    phone = fields.Char(required=True, index=True, help='Destination, E.164.')
    code_hash = fields.Char(required=True, help='HMAC-SHA256 of the code.')
    expires_at = fields.Datetime(required=True, index=True)
    attempts = fields.Integer(default=0, help='Failed verify attempts.')
    verified = fields.Boolean(default=False, index=True)
    channel = fields.Selection(
        [('telnyx', 'Telnyx SMS'), ('bluebubbles', 'iMessage (BlueBubbles)')],
        readonly=True, help='Transport that accepted the delivery.',
    )
    request_ip = fields.Char(readonly=True, help='Requesting client IP (audit).')

    # ------------------------------------------------------------------
    # Hashing
    # ------------------------------------------------------------------
    @api.model
    def _hash_code(self, phone, code):
        """Keyed hash so a DB read alone can't recover live codes.

        Keyed with database.secret (same trust anchor as the lead tokens in
        controllers/auth.py) and bound to the phone so a hash copied between
        rows can never verify for another number.
        """
        secret = (self.env['ir.config_parameter'].sudo()
                  .get_param('database.secret', '') or '')
        msg = ('%s|%s' % (phone, code)).encode('utf-8')
        return hmac.new(secret.encode('utf-8'), msg, hashlib.sha256).hexdigest()

    # ------------------------------------------------------------------
    # Issue
    # ------------------------------------------------------------------
    @api.model
    def issue(self, phone, request_ip=''):
        """Create + deliver a fresh code for `phone` (E.164).

        Returns (ok: bool, error: str|None, status: int). Rate limits:
        60s cooldown between sends and 5 sends/hour per number — the
        controller's FE-key gate plus the frontend's own per-IP throttle
        stand in front of this.
        """
        now = fields.Datetime.now()
        self._purge_stale(now)

        recent = self.sudo().search([('phone', '=', phone)],
                                    order='create_date desc', limit=1)
        if recent and (now - recent.create_date).total_seconds() < OTP_RESEND_COOLDOWN_SECONDS:
            return False, 'Please wait a minute before requesting another code.', 429

        hour_ago = fields.Datetime.subtract(now, hours=1)
        sent_last_hour = self.sudo().search_count([
            ('phone', '=', phone),
            ('create_date', '>=', hour_ago),
        ])
        if sent_last_hour >= OTP_MAX_PER_PHONE_PER_HOUR:
            return False, 'Too many codes requested for this number. Try again later.', 429

        # secrets.randbelow → uniform, crypto-grade; zero-padded to length.
        code = str(secrets.randbelow(10 ** OTP_LENGTH)).zfill(OTP_LENGTH)

        ok, channel, err = self._deliver(phone, OTP_BODY % code)
        if not ok:
            _logger.warning('OTP delivery failed for %s: %s', phone, err)
            return False, 'Could not send a verification code right now.', 502

        # Supersede any live prior codes for this phone — exactly one code is
        # ever verifiable per number, the one delivered last.
        self.sudo().search([
            ('phone', '=', phone),
            ('verified', '=', False),
        ]).write({'expires_at': now})

        self.sudo().create({
            'phone': phone,
            'code_hash': self._hash_code(phone, code),
            'expires_at': fields.Datetime.add(now, minutes=OTP_TTL_MINUTES),
            'channel': channel,
            'request_ip': request_ip or '',
        })
        return True, None, 200

    # ------------------------------------------------------------------
    # Check
    # ------------------------------------------------------------------
    @api.model
    def check(self, phone, code):
        """Verify `code` for `phone`. Single-use; attempt-capped.

        Returns (ok: bool, error: str|None). The row is consumed on success
        (verified=True) so a captured code can't be replayed.
        """
        now = fields.Datetime.now()
        rec = self.sudo().search([
            ('phone', '=', phone),
            ('verified', '=', False),
            ('expires_at', '>', now),
        ], order='create_date desc', limit=1)
        if not rec:
            return False, 'Code expired or not found. Request a new one.'
        if rec.attempts >= OTP_MAX_ATTEMPTS:
            return False, 'Too many incorrect attempts. Request a new code.'

        if not hmac.compare_digest(rec.code_hash,
                                   self._hash_code(phone, code or '')):
            rec.attempts += 1
            return False, 'Incorrect code.'

        rec.verified = True
        return True, None

    @api.model
    def _purge_stale(self, now):
        """Drop rows past retention — they are secrets with no audit value."""
        cutoff = fields.Datetime.subtract(now, hours=OTP_RETENTION_HOURS)
        stale = self.sudo().search([('create_date', '<', cutoff)], limit=500)
        if stale:
            stale.unlink()

    # ------------------------------------------------------------------
    # Delivery — transport chain. ICP keys are read directly rather than
    # depending on mint_sms_telnyx: the params are plain config, and coupling
    # the signup API to the SMS module would make it uninstallable without it.
    # ------------------------------------------------------------------
    @api.model
    def _deliver(self, phone, body):
        """Try Telnyx, then BlueBubbles. Returns (ok, channel, error)."""
        errors = []

        ok, err = self._send_telnyx(phone, body)
        if ok:
            return True, 'telnyx', None
        if err:
            errors.append('telnyx: %s' % err)

        ok, err = self._send_bluebubbles(phone, body)
        if ok:
            return True, 'bluebubbles', None
        if err:
            errors.append('bluebubbles: %s' % err)

        return False, None, '; '.join(errors) or 'no SMS transport configured'

    @api.model
    def _send_telnyx(self, phone, body):
        """Direct Telnyx send. (None-error when the gateway is disabled.)"""
        ICP = self.env['ir.config_parameter'].sudo()
        if ICP.get_param('mint_sms_telnyx.enabled') != 'True':
            return False, None
        api_key = ICP.get_param('mint_sms_telnyx.api_key', '')
        from_number = ICP.get_param('mint_sms_telnyx.from_number', '')
        profile_id = ICP.get_param('mint_sms_telnyx.messaging_profile_id', '')
        base = ICP.get_param('mint_sms_telnyx.api_base', 'https://api.telnyx.com/v2')
        if not api_key or not from_number:
            return False, 'enabled but api_key/from_number unset'
        payload = {'from': from_number, 'to': phone, 'text': body}
        if profile_id:
            payload['messaging_profile_id'] = profile_id
        try:
            resp = requests.post(
                base.rstrip('/') + '/messages',
                headers={'Authorization': 'Bearer %s' % api_key,
                         'Content-Type': 'application/json'},
                data=json.dumps(payload),
                timeout=SEND_TIMEOUT,
            )
        except requests.RequestException as e:
            return False, str(e)
        if resp.status_code < 400:
            return True, None
        return False, 'HTTP %s' % resp.status_code

    @api.model
    def _send_bluebubbles(self, phone, body):
        """iMessage bridge send, whitelisting the number at the proxy first.

        The proxy in front of BlueBubbles 403s numbers missing from its OWN
        whitelist (see mint_sms_telnyx res_partner._sync_proxy_whitelist), and
        an OTP destination is by definition a number Odoo has never consented
        — so add it before sending. Both calls are direct because there is no
        res.partner yet to hang the consent helpers off.
        """
        ICP = self.env['ir.config_parameter'].sudo()
        if ICP.get_param('mint_sms_telnyx.bluebubbles_enabled') != 'True':
            return False, None
        base = (ICP.get_param('mint_sms_telnyx.bluebubbles_url', '') or '').rstrip('/')
        password = ICP.get_param('mint_sms_telnyx.bluebubbles_password', '')
        wl_token = ICP.get_param('mint_sms_telnyx.proxy_whitelist_token', '')
        if not base or not password:
            return False, 'enabled but url/password unset'

        if wl_token:
            try:
                requests.post(
                    base + '/admin/whitelist',
                    json={'number': phone, 'action': 'add'},
                    headers={'Authorization': 'Bearer %s' % wl_token,
                             'ngrok-skip-browser-warning': 'true'},
                    timeout=SEND_TIMEOUT,
                )
            except requests.RequestException as e:
                # Keep going: the number may already be whitelisted.
                _logger.warning('OTP proxy whitelist add failed for %s: %s',
                                phone, e)

        try:
            resp = requests.post(
                base + '/api/v1/message/text',
                params={'password': password},
                headers={'Content-Type': 'application/json',
                         'ngrok-skip-browser-warning': 'true'},
                data=json.dumps({
                    'chatGuid': 'iMessage;-;%s' % phone,
                    'message': body,
                    'method': 'apple-script',
                    'tempGuid': 'mint-otp-%s' % secrets.token_hex(8),
                }),
                timeout=SEND_TIMEOUT,
            )
        except requests.RequestException as e:
            return False, str(e)
        if resp.status_code < 400:
            return True, None
        return False, 'HTTP %s' % resp.status_code
