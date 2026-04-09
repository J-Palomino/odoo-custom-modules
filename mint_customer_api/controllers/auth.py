# -*- coding: utf-8 -*-
"""
Customer authentication endpoints for MintDeals frontend.

All endpoints return JSON, use auth='none' since we handle auth via JWT.
"""
import json
import logging

from odoo import http
from odoo.http import request, Response
from odoo.exceptions import AccessDenied, UserError

_logger = logging.getLogger(__name__)


def json_response(data, status=200):
    """JSON response with CORS headers."""
    return Response(
        json.dumps(data, default=str),
        status=status,
        content_type='application/json',
        headers={
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'GET, POST, PUT, OPTIONS',
            'Access-Control-Allow-Headers': 'Content-Type, Authorization',
        }
    )


def error_response(message, status=400):
    return json_response({'error': message}, status=status)


def _get_bearer_token():
    """Extract JWT from Authorization header."""
    auth = request.httprequest.headers.get('Authorization', '')
    if auth.startswith('Bearer '):
        return auth[7:]
    return None


def _verify_and_get_user():
    """Verify JWT and return the user record, or None."""
    token = _get_bearer_token()
    if not token:
        return None
    payload = request.env['res.users'].sudo()._verify_jwt(token)
    if not payload:
        return None
    user = request.env['res.users'].sudo().browse(payload.get('user_id'))
    if not user.exists():
        return None
    return user


class MintCustomerAuth(http.Controller):
    """Customer authentication controller."""

    @http.route('/api/v1/auth/login', type='http', auth='none',
                methods=['POST', 'OPTIONS'], csrf=False, cors='*')
    def login(self, **kw):
        """Authenticate with email + password, return JWT."""
        if request.httprequest.method == 'OPTIONS':
            return json_response({})

        try:
            data = json.loads(request.httprequest.data)
        except (json.JSONDecodeError, TypeError):
            return error_response('Invalid JSON body')

        email = data.get('email', '').strip().lower()
        password = data.get('password', '')

        if not email or not password:
            return error_response('Email and password are required')

        user = request.env['res.users'].sudo().search(
            [('login', '=', email), ('active', '=', True)], limit=1,
        )
        if not user:
            return error_response('Invalid email or password', 401)

        # Verify password using passlib (Odoo's internal hasher)
        try:
            from passlib.context import CryptContext
            ctx = CryptContext(['pbkdf2_sha512', 'plaintext'], deprecated=['plaintext'])
            request.env.cr.execute(
                "SELECT COALESCE(password, '') FROM res_users WHERE id=%s",
                (user.id,)
            )
            hashed = request.env.cr.fetchone()[0]
            if not hashed or not ctx.verify(password, hashed):
                return error_response('Invalid email or password', 401)
        except Exception:
            return error_response('Invalid email or password', 401)
        token = user._generate_jwt()

        return json_response({
            'token': token,
            'user': {
                'id': user.id,
                'name': user.partner_id.name,
                'email': user.login,
                'phone': user.partner_id.phone or user.partner_id.mobile or '',
                'partner_id': user.partner_id.id,
            },
        })

    @http.route('/api/v1/auth/register', type='http', auth='none',
                methods=['POST', 'OPTIONS'], csrf=False, cors='*')
    def register(self, **kw):
        """Create a new portal user and return JWT."""
        if request.httprequest.method == 'OPTIONS':
            return json_response({})

        try:
            data = json.loads(request.httprequest.data)
        except (json.JSONDecodeError, TypeError):
            return error_response('Invalid JSON body')

        name = data.get('name', '').strip()
        email = data.get('email', '').strip().lower()
        password = data.get('password', '')
        phone = data.get('phone', '').strip()

        if not name or not email or not password:
            return error_response('Name, email, and password are required')

        if len(password) < 8:
            return error_response('Password must be at least 8 characters')

        # Check if user already exists
        existing = request.env['res.users'].sudo().search(
            [('login', '=', email)], limit=1
        )
        if existing:
            return error_response('An account with this email already exists', 409)

        try:
            # Create user via raw SQL + ORM hybrid (Odoo 19 create hooks
            # cause transaction rollbacks due to mail/signup side effects)
            main_company = request.env['res.company'].sudo().browse(1)

            # Create partner first
            partner = request.env['res.partner'].sudo().with_context(
                mail_create_nosubscribe=True,
                mail_create_nolog=True,
                tracking_disable=True,
                mail_notrack=True,
            ).create({
                'name': name,
                'email': email,
                'phone': phone or False,
                'customer_rank': 1,
                'company_id': main_company.id,
            })
            request.env.cr.flush()

            # Create user with ALL hooks disabled
            request.env.cr.execute("""
                INSERT INTO res_users (login, password, company_id, partner_id,
                                       active, share, create_uid, write_uid,
                                       create_date, write_date, notification_type)
                VALUES (%s, '', %s, %s, true, true, 1, 1, NOW(), NOW(), 'email')
                RETURNING id
            """, (email, main_company.id, partner.id))
            user_id = request.env.cr.fetchone()[0]

            # Add company_ids m2m relation
            request.env.cr.execute("""
                INSERT INTO res_company_users_rel (cid, user_id)
                VALUES (%s, %s)
                ON CONFLICT DO NOTHING
            """, (main_company.id, user_id))

            # Invalidate ORM cache and set password (handles hashing)
            request.env['res.users'].sudo().invalidate_model()
            user = request.env['res.users'].sudo().browse(user_id)
            user.with_context(
                no_reset_password=True,
                tracking_disable=True,
            ).write({'password': password})

            token = user._generate_jwt()

            return json_response({
                'token': token,
                'user': {
                    'id': user.id,
                    'name': user.partner_id.name,
                    'email': user.login,
                    'phone': user.partner_id.phone or user.partner_id.mobile or '',
                    'partner_id': user.partner_id.id,
                },
            }, status=201)

        except UserError as e:
            return error_response(str(e))
        except Exception as e:
            _logger.exception('Registration failed: %s', e)
            return error_response('Registration failed: %s' % str(e), 500)

    @http.route('/api/v1/auth/forgot-password', type='http', auth='none',
                methods=['POST', 'OPTIONS'], csrf=False, cors='*')
    def forgot_password(self, **kw):
        """Trigger Odoo password reset email."""
        if request.httprequest.method == 'OPTIONS':
            return json_response({})

        try:
            data = json.loads(request.httprequest.data)
        except (json.JSONDecodeError, TypeError):
            return error_response('Invalid JSON body')

        email = data.get('email', '').strip().lower()
        if not email:
            return error_response('Email is required')

        # Always return success to prevent email enumeration
        try:
            user = request.env['res.users'].sudo().search(
                [('login', '=', email)], limit=1
            )
            if user:
                user.action_reset_password()
        except Exception:
            _logger.exception('Password reset failed for %s', email)

        return json_response({
            'message': 'If an account exists with that email, a reset link has been sent.',
        })

    @http.route('/api/v1/auth/verify', type='http', auth='none',
                methods=['GET', 'OPTIONS'], csrf=False, cors='*')
    def verify(self, **kw):
        """Validate JWT from Authorization header."""
        if request.httprequest.method == 'OPTIONS':
            return json_response({})

        user = _verify_and_get_user()
        if not user:
            return error_response('Invalid or expired token', 401)

        return json_response({
            'user': {
                'id': user.id,
                'name': user.partner_id.name,
                'email': user.login,
                'phone': user.partner_id.phone or user.partner_id.mobile or '',
                'partner_id': user.partner_id.id,
            },
        })
