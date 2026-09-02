# -*- coding: utf-8 -*-
import logging
import time

import jwt

from odoo import _, models, fields, api
from odoo.exceptions import ValidationError

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
        # Re-check only when something that can flip a user internal changed.
        # res.users.write also runs on ordinary login bookkeeping; this keeps
        # the guard off that path.
        if {'group_ids', 'partner_id', 'active'} & set(vals):
            self._mint_check_customer_not_internal()
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

    # ------------------------------------------------------------------
    # Customers must never hold an internal user account
    # ------------------------------------------------------------------
    # An internal (non-share) user reads the backend: other customers' PII,
    # tickets, inventory, HR. Customers get portal/share accounts instead.
    # Beyond the obvious exposure, an internal account also breaks the
    # customer-isolation rule for everyone else — the partner stops being
    # hidden, and starts turning up in internal pickers such as ticket
    # followers.
    #
    # Employees who also shop here are unaffected: _mint_is_customer treats
    # any employee work-contact as staff, so it returns False for them. If a
    # genuine customer really is being hired, mark their contact as an
    # employee first — that is the deliberate, auditable escape hatch.

    def _mint_check_customer_not_internal(self):
        for user in self:
            # Inactive users are skipped so archiving stays possible: if a
            # customer ever does end up with an internal account, archiving it
            # is the fix, and a guard that blocked its own remediation would
            # be worse than the problem.
            if user.share or not user.active or not user.partner_id:
                continue
            # ignore_users=user: the account being granted already exists at
            # this point, so it must not count as evidence that its own
            # partner is staff.
            if user.partner_id.sudo()._mint_is_customer(ignore_users=user):
                raise ValidationError(_(
                    "%(name)s is a customer and cannot be given an internal "
                    "user account.\n\n"
                    "Customers get portal access, not backend access. If this "
                    "person is actually staff, mark their contact as an "
                    "employee first, then create the account.",
                    name=user.partner_id.display_name,
                ))

    @api.model_create_multi
    def create(self, vals_list):
        users = super().create(vals_list)
        users._mint_check_customer_not_internal()
        return users
