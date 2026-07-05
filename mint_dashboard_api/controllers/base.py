# -*- coding: utf-8 -*-
"""Shared response helpers + employee auth decorator for the dashboard API.

Response/CORS pattern follows mint_api_v2/controllers/api.py; JWT verification
reuses mint_customer_api's res.users._verify_jwt (session-version revocation
included). Every domain route MUST go through require_dashboard_auth — the
per-route permission check is a day-one design constraint from the customer
API's P0b IDOR review (commit 656ee5a).
"""
import functools
import json
import logging

from odoo.http import request, Response

_logger = logging.getLogger(__name__)

WEB_LOGIN_PREFIX = 'web:'


def json_response(data, status=200):
    return Response(
        json.dumps(data, default=str),
        status=status,
        content_type='application/json',
        headers={
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'GET, POST, PUT, DELETE, OPTIONS',
            'Access-Control-Allow-Headers': 'Content-Type, Authorization, X-Api-Key',
        },
    )


def error_response(message, status=400):
    return json_response({'error': message, 'status': status}, status=status)


def _bearer_token():
    auth = request.httprequest.headers.get('Authorization', '')
    if auth.startswith('Bearer '):
        return auth[7:]
    return None


def verify_employee():
    """Return the authenticated internal user, or None.

    Employee-only surface: portal/share users and web:-prefixed customer
    logins can never authenticate here, regardless of token validity.
    """
    token = _bearer_token()
    if not token:
        return None
    payload = request.env['res.users'].sudo()._verify_jwt(token)
    if not payload:
        return None
    user = request.env['res.users'].sudo().browse(payload.get('user_id'))
    if not user.exists() or not user.active:
        return None
    if user.share or (user.login or '').startswith(WEB_LOGIN_PREFIX):
        return None
    return user


def permission_set(user):
    return request.env['mint.dashboard.permission'].sudo().search(
        [('user_id', '=', user.id), ('active', '=', True)], limit=1)


def require_dashboard_auth(module=None, action='view'):
    """Route decorator: JWT employee auth + optional module/action gate.

    Stashes request.dashboard_user / request.dashboard_perm for the handler.
    System admins (base.group_system) bypass the module gate but still need
    a valid employee token.
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(self, *args, **kwargs):
            if request.httprequest.method == 'OPTIONS':
                return json_response({})
            user = verify_employee()
            if not user:
                return error_response('Unauthorized', 401)
            perm = permission_set(user)
            is_admin = user.has_group('base.group_system')
            if not perm and not is_admin:
                return error_response('No dashboard permission set', 403)
            if module and not is_admin and not perm.has_module_permission(module, action):
                return error_response(
                    'Forbidden: missing %s permission on %s' % (action, module), 403)
            request.dashboard_user = user
            request.dashboard_perm = perm
            return fn(self, *args, **kwargs)
        return wrapper
    return decorator


def allowed_market_slugs():
    """Market slugs the current request may see (None = unrestricted admin)."""
    user = getattr(request, 'dashboard_user', None)
    perm = getattr(request, 'dashboard_perm', None)
    if user is not None and user.has_group('base.group_system'):
        return None
    return perm.market_slugs() if perm else []
