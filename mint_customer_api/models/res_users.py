# -*- coding: utf-8 -*-
import logging
import time

import jwt

from odoo import models, fields, api

_logger = logging.getLogger(__name__)

JWT_ALGORITHM = 'HS256'
JWT_EXPIRY_SECONDS = 7 * 24 * 60 * 60  # 7 days


class ResUsers(models.Model):
    _inherit = 'res.users'

    # Session revocation epoch. Every issued JWT carries the value current at
    # issue time ('sv'); _verify_jwt rejects any token whose 'sv' is stale.
    # Bumping this invalidates ALL of the user's outstanding tokens at once —
    # the kill-switch for "log out everywhere", password change, or a
    # compromised account. (Legacy tokens minted before this field shipped
    # carry no 'sv' and are grandfathered until they expire, ≤7 days.)
    session_version = fields.Integer(
        string='Session Version', default=1, copy=False,
        help='Bump to revoke all of this user\'s outstanding JWT sessions.',
    )

    # Sign in with Apple stable subject. Apple's `sub` claim is the ONLY
    # durable identifier for an Apple user: with Hide My Email the id_token
    # carries a per-app private-relay address the customer can disable at any
    # time, so an email-keyed lookup (the Google pattern) would mint a second
    # account the day the relay changes. /api/v1/auth/apple therefore matches
    # on this field first and only falls back to email for first sign-ins.
    apple_sub = fields.Char(
        string='Apple Sign-In Subject', index=True, copy=False, readonly=True,
        help="Stable `sub` claim from Apple's identity token. Set on first "
             "Sign in with Apple; used as the primary lookup key thereafter.",
    )

    def revoke_sessions(self):
        """Invalidate all outstanding JWT sessions for these users."""
        for user in self:
            user.sudo().session_version = (user.session_version or 1) + 1
        return True

    def write(self, vals):
        # A password change must invalidate existing sessions (a reset or a
        # rotation should not leave old tokens valid).
        res = super().write(vals)
        if 'password' in vals and not self.env.context.get('mint_skip_session_bump'):
            for user in self:
                user.sudo().session_version = (user.session_version or 1) + 1
        return res

    def _get_jwt_secret(self):
        """Read JWT secret from system parameters, generate if missing."""
        ICP = self.env['ir.config_parameter'].sudo()
        secret = ICP.get_param('mint.jwt_secret')
        if not secret:
            import secrets
            secret = secrets.token_hex(32)
            ICP.set_param('mint.jwt_secret', secret)
        return secret

    def _generate_jwt(self):
        """Generate a JWT token for this user."""
        self.ensure_one()
        secret = self._get_jwt_secret()
        now = int(time.time())
        payload = {
            'user_id': self.id,
            'partner_id': self.partner_id.id,
            'email': self.login,
            # `or 1` keeps sv a real int even for raw-SQL-inserted web users
            # whose column is NULL (the raw INSERT bypasses the ORM default).
            'sv': self.session_version or 1,
            'iat': now,
            'exp': now + JWT_EXPIRY_SECONDS,
        }
        return jwt.encode(payload, secret, algorithm=JWT_ALGORITHM)

    @api.model
    def _verify_jwt(self, token):
        """Verify a JWT and return its payload, or None if invalid/revoked."""
        secret = self.env['res.users']._get_jwt_secret()
        try:
            payload = jwt.decode(token, secret, algorithms=[JWT_ALGORITHM])
        except jwt.ExpiredSignatureError:
            _logger.info('JWT expired')
            return None
        except jwt.InvalidTokenError as e:
            _logger.info('Invalid JWT: %s', e)
            return None

        # Revocation check: a token is only valid while its 'sv' matches the
        # user's current session_version. Tokens minted before this field
        # existed carry no 'sv' and are grandfathered until natural expiry.
        token_sv = payload.get('sv')
        if token_sv is not None:
            user = self.env['res.users'].sudo().browse(payload.get('user_id'))
            if not user.exists() or (user.session_version or 1) != token_sv:
                _logger.info('JWT revoked (session_version mismatch) for user %s', payload.get('user_id'))
                return None
        return payload
