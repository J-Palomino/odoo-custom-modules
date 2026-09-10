# -*- coding: utf-8 -*-
"""Employee login for the marketing dashboard.

Credential check mirrors mint_customer_api/controllers/auth.py login, with the
scope inverted: only internal users (share=False) may authenticate, and
web:-prefixed customer logins are rejected outright.
"""
import json
import logging

from odoo import http
from odoo.http import request

from .base import (
    WEB_LOGIN_PREFIX,
    error_response,
    json_response,
    permission_set,
    require_dashboard_auth,
    verify_employee,
)

_logger = logging.getLogger(__name__)


def _profile_payload(user):
    perm = permission_set(user)
    return {
        'id': user.id,
        'name': user.partner_id.name or user.login,
        'email': user.partner_id.email or user.login,
        'avatar_url': None,
        'permissions': perm.as_payload() if perm else None,
        'is_admin': user.has_group('base.group_system'),
    }


class MintDashboardAuth(http.Controller):

    @http.route('/api/v1/dashboard/auth/login', type='http', auth='none',
                methods=['POST', 'OPTIONS'], csrf=False, cors='*')
    def login(self, **kw):
        if request.httprequest.method == 'OPTIONS':
            return json_response({})

        # Optional FE gate: only enforced when the config param is set. The
        # dashboard is a pure SPA (no server to hold a secret), so this is a
        # soft gate for staging/rollout, not the security boundary — that is
        # the credential check + JWT below.
        ICP = request.env['ir.config_parameter'].sudo()
        fe_key = ICP.get_param('mint_dashboard_api.fe_api_key')
        if fe_key and request.httprequest.headers.get('X-Api-Key') != fe_key:
            return error_response('Forbidden', 403)

        try:
            data = json.loads(request.httprequest.data)
        except (json.JSONDecodeError, TypeError):
            return error_response('Invalid JSON body')

        email = (data.get('email') or '').strip().lower()
        password = data.get('password') or ''
        if not email or not password:
            return error_response('Email and password are required')

        # Employee-only surface: customer identities are rejected before any
        # credential work, so the response never leaks whether they exist.
        if email.startswith(WEB_LOGIN_PREFIX):
            return error_response('Customer accounts cannot access the dashboard', 403)

        user = request.env['res.users'].sudo().search(
            [('login', '=', email), ('share', '=', False), ('active', '=', True)],
            limit=1)
        if not user:
            return error_response('Invalid email or password', 401)

        try:
            from passlib.context import CryptContext
            ctx = CryptContext(['pbkdf2_sha512', 'plaintext'], deprecated=['plaintext'])
            request.env.cr.execute(
                "SELECT COALESCE(password, '') FROM res_users WHERE id=%s", (user.id,))
            hashed = request.env.cr.fetchone()[0]
            if not hashed or not ctx.verify(password, hashed):
                return error_response('Invalid email or password', 401)
        except Exception:
            return error_response('Invalid email or password', 401)

        token = user._generate_jwt()
        return json_response({'token': token, 'user': _profile_payload(user)})

    @http.route('/api/v1/dashboard/auth/me', type='http', auth='none',
                methods=['GET', 'OPTIONS'], csrf=False, cors='*')
    def me(self, **kw):
        if request.httprequest.method == 'OPTIONS':
            return json_response({})
        user = verify_employee()
        if not user:
            return error_response('Unauthorized', 401)
        return json_response({'user': _profile_payload(user)})

    @http.route('/api/v1/dashboard/auth/logout', type='http', auth='none',
                methods=['POST', 'OPTIONS'], csrf=False, cors='*')
    @require_dashboard_auth()
    def logout(self, **kw):
        # Bumping session_version revokes every outstanding JWT for the user
        # (mint_customer_api res.users.revoke_sessions).
        request.dashboard_user.revoke_sessions()
        return json_response({'ok': True})
