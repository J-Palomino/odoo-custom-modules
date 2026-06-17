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

# Web-signup user logins are prefixed with "web:" to isolate them from
# internal employee accounts (login = plain email). An employee can shop
# on the consumer site without colliding with their staff identity.
WEB_LOGIN_PREFIX = 'web:'


def _web_login(email):
    """Return the prefixed res.users.login for a web customer email."""
    return WEB_LOGIN_PREFIX + (email or '').strip().lower()


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

        # Front-end gate: only the MintDeals frontend (which holds the rpc
        # API key) may reach this endpoint. Blocks direct hits to the backend
        # that would otherwise bypass the FE's honeypot, timing token, bot
        # score and per-account/per-IP brute-force throttling.
        gate = self._require_fe_key()
        if gate:
            return gate

        try:
            data = json.loads(request.httprequest.data)
        except (json.JSONDecodeError, TypeError):
            return error_response('Invalid JSON body')

        email = data.get('email', '').strip().lower()
        password = data.get('password', '')

        if not email or not password:
            return error_response('Email and password are required')

        # Scope auth to share=True (portal/web customers) so an internal
        # employee's credentials can never log in through the consumer site.
        # Accept both prefixed (new) and plain (legacy) logins for compat.
        user = request.env['res.users'].sudo().search(
            [
                ('login', 'in', [email, _web_login(email)]),
                ('share', '=', True),
                ('active', '=', True),
            ],
            limit=1,
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
                'email': user.partner_id.email or user.login.removeprefix(WEB_LOGIN_PREFIX),
                'phone': user.partner_id.phone or '',
                'partner_id': user.partner_id.id,
            },
        })

    @http.route('/api/v1/auth/register', type='http', auth='none',
                methods=['POST', 'OPTIONS'], csrf=False, cors='*')
    def register(self, **kw):
        """Create a new portal user and return JWT."""
        if request.httprequest.method == 'OPTIONS':
            return json_response({})

        # Front-end gate: only the MintDeals frontend (which holds the rpc
        # API key) may reach this endpoint. Blocks direct hits to the backend
        # that would otherwise bypass the FE's honeypot, timing token, bot
        # score and per-IP daily registration cap (mass account creation).
        gate = self._require_fe_key()
        if gate:
            return gate

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

        # Server-side 21+ enforcement (defense in depth). The FE form checks
        # age client-side, but a manipulated client could submit any DOB, so
        # the account creator must verify it too. DOB is sent as ISO yyyy-mm-dd.
        from datetime import date
        dob_raw = (data.get('dateOfBirth') or data.get('date_of_birth') or '').strip()
        if not dob_raw:
            return error_response('Date of birth is required')
        try:
            dob = date.fromisoformat(dob_raw[:10])
        except (ValueError, TypeError):
            return error_response('Invalid date of birth')
        today = date.today()
        age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
        if age < 21:
            return error_response('You must be 21 or older to create an account', 403)

        # Only block on existing WEB customer account. An internal employee
        # with login=<email> (share=False) must still be able to sign up as
        # a separate customer (their work identity stays disjoint).
        existing = request.env['res.users'].sudo().search(
            [('login', '=', _web_login(email))], limit=1
        )
        if existing:
            return error_response('An account with this email already exists', 409)

        try:
            user = self._create_web_user(email=email, name=name, phone=phone)
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
                    'email': user.partner_id.email or user.login.removeprefix(WEB_LOGIN_PREFIX),
                    'phone': user.partner_id.phone or '',
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

        # Front-end gate: only the MintDeals frontend (which holds the rpc
        # API key) may reach this endpoint. Blocks direct hits to the backend
        # that would otherwise bypass the FE's origin check and per-IP /
        # per-target throttle (reset-email bombing).
        gate = self._require_fe_key()
        if gate:
            return gate

        try:
            data = json.loads(request.httprequest.data)
        except (json.JSONDecodeError, TypeError):
            return error_response('Invalid JSON body')

        email = data.get('email', '').strip().lower()
        if not email:
            return error_response('Email is required')

        # Always return success to prevent email enumeration. Scope to
        # share=True so resetting a web customer password can't touch an
        # internal employee's credentials (the consumer site only ever
        # resets consumer accounts).
        try:
            user = request.env['res.users'].sudo().search(
                [
                    ('login', 'in', [email, _web_login(email)]),
                    ('share', '=', True),
                ],
                limit=1,
            )
            if user:
                user.action_reset_password()
        except Exception:
            _logger.exception('Password reset failed for %s', email)

        return json_response({
            'message': 'If an account exists with that email, a reset link has been sent.',
        })

    @http.route('/api/v1/auth/google', type='http', auth='none',
                methods=['POST', 'OPTIONS'], csrf=False, cors='*')
    def google_auth(self, **kw):
        """Sign-in with Google. Called server-to-server by the Astro callback
        after it exchanges the OAuth code with Google. Trust on Odoo side
        relies on X-Api-Key (only the frontend server holds it) plus the
        fact that the frontend already validated the id_token via Google's
        token endpoint using the client secret."""
        if request.httprequest.method == 'OPTIONS':
            return json_response({})

        gate = self._require_fe_key()
        if gate:
            return gate

        try:
            data = json.loads(request.httprequest.data)
        except (json.JSONDecodeError, TypeError):
            return error_response('Invalid JSON body')

        email = data.get('email', '').strip().lower()
        name = (data.get('name') or '').strip() or (email.split('@')[0] if email else '')
        google_sub = (data.get('google_sub') or '').strip()

        if not email or not google_sub:
            return error_response('email and google_sub are required')

        # Find existing web customer (prefixed or legacy). share=True ensures
        # we never return an internal employee account for a matching email.
        user = request.env['res.users'].sudo().search(
            [
                ('login', 'in', [email, _web_login(email)]),
                ('share', '=', True),
                ('active', '=', True),
            ],
            limit=1,
        )

        if not user:
            try:
                user = self._create_web_user(email=email, name=name)
            except Exception as e:
                _logger.exception('Google OAuth account creation failed: %s', e)
                return error_response('Could not create account', 500)

        token = user._generate_jwt()
        return json_response({
            'token': token,
            'user': {
                'id': user.id,
                'name': user.partner_id.name,
                'email': user.partner_id.email or user.login.removeprefix(WEB_LOGIN_PREFIX),
                'phone': user.partner_id.phone or '',
                'partner_id': user.partner_id.id,
            },
        })

    def _require_fe_key(self):
        """Reject the request unless it carries the FE-shared rpc X-Api-Key.

        Only the MintDeals frontend holds this key, so gating on it blocks
        direct hits to the backend that would bypass the FE's honeypot, timing
        token, bot score and rate limiting. Returns an error Response to return,
        or None when the caller is authorized.
        """
        api_key = request.httprequest.headers.get('X-Api-Key', '')
        if not api_key or not self._verify_fe_api_key(api_key):
            return error_response('Unauthorized', 401)
        return None

    def _verify_fe_api_key(self, key):
        """Verify X-Api-Key against Odoo's res.users.apikeys store."""
        try:
            uid = request.env['res.users.apikeys'].sudo()._check_credentials(
                scope='rpc', key=key,
            )
            return bool(uid)
        except Exception:
            return False

    def _create_web_user(self, email, name, phone=''):
        """Create a fresh partner + portal user for a web-site signup.

        Always creates a NEW res.partner (doesn't reuse an existing one that
        happens to have the same email) so employees shopping the consumer
        site get a separate customer identity from their staff partner.

        Uses login = 'web:<email>' so web users never collide with internal
        user accounts (login = plain email) and sets is_web_customer=True
        so record rules restrict PII to privileged groups.
        """
        main_company = request.env['res.company'].sudo().browse(1)
        login = _web_login(email)

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
            'is_web_customer': True,
        })
        request.env.cr.flush()

        # Raw INSERT to bypass signup/mail hooks that conflict with the
        # transaction. ON CONFLICT handles the race where two concurrent
        # signups for the same email both try to insert — the loser
        # re-selects the winning row.
        #
        # FIXME: This bypasses the website module's @api.constrains check
        # for duplicate logins where website_id IS NULL, so the loser-
        # branch's password write can clobber the winner's account on a
        # genuine race. Tracked for ORM refactor — see follow-up ticket.
        # Constraint is named (not column-targeted) because the website
        # module replaces UNIQUE (login) with UNIQUE (login, website_id).
        request.env.cr.execute("""
            INSERT INTO res_users (login, password, company_id, partner_id,
                                   active, share, create_uid, write_uid,
                                   create_date, write_date, notification_type)
            VALUES (%s, '', %s, %s, true, true, 1, 1, NOW(), NOW(), 'email')
            ON CONFLICT ON CONSTRAINT res_users_login_key DO NOTHING
            RETURNING id
        """, (login, main_company.id, partner.id))
        row = request.env.cr.fetchone()
        if row:
            user_id = row[0]
        else:
            # Race: another request just created this login. Use theirs and
            # drop the extra partner we made to avoid orphans.
            request.env.cr.execute(
                "SELECT id FROM res_users WHERE login = %s", (login,)
            )
            user_id = request.env.cr.fetchone()[0]
            partner.sudo().unlink()

        request.env.cr.execute("""
            INSERT INTO res_company_users_rel (cid, user_id)
            VALUES (%s, %s)
            ON CONFLICT DO NOTHING
        """, (main_company.id, user_id))

        request.env['res.users'].sudo().invalidate_model()
        return request.env['res.users'].sudo().browse(user_id)

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
                'email': user.partner_id.email or user.login.removeprefix(WEB_LOGIN_PREFIX),
                'phone': user.partner_id.phone or '',
                'partner_id': user.partner_id.id,
            },
        })
