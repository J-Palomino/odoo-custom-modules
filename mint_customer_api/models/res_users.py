# -*- coding: utf-8 -*-
import logging
import time

import jwt

from odoo import models, api

_logger = logging.getLogger(__name__)

JWT_ALGORITHM = 'HS256'
JWT_EXPIRY_SECONDS = 7 * 24 * 60 * 60  # 7 days


class ResUsers(models.Model):
    _inherit = 'res.users'

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
            'iat': now,
            'exp': now + JWT_EXPIRY_SECONDS,
        }
        return jwt.encode(payload, secret, algorithm=JWT_ALGORITHM)

    @api.model
    def _verify_jwt(self, token):
        """Verify a JWT and return its payload, or None if invalid."""
        secret = self.env['res.users']._get_jwt_secret()
        try:
            payload = jwt.decode(token, secret, algorithms=[JWT_ALGORITHM])
            return payload
        except jwt.ExpiredSignatureError:
            _logger.info('JWT expired')
            return None
        except jwt.InvalidTokenError as e:
            _logger.info('Invalid JWT: %s', e)
            return None
