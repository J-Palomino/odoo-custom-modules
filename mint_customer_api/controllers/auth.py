# -*- coding: utf-8 -*-
"""
Customer authentication endpoints for MintDeals frontend.

All endpoints return JSON, use auth='none' since we handle auth via JWT.
"""
import json
import logging
import threading
from datetime import datetime

import urllib.request
import urllib.error

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


def _enqueue_dutchie_create(payload):
    """Fire-and-forget POST to the inventory-service BullMQ enqueue endpoint.

    Runs in a background thread so signup response is not blocked by the
    network call. Inventory service URL + shared secret come from
    ir.config_parameter; if unset, enqueue is skipped and the caller relies
    on the nightly Dutchie import to eventually sync the customer.
    """
    ICP = request.env['ir.config_parameter'].sudo()
    base_url = (ICP.get_param('mint.inventory_service_url') or '').rstrip('/')
    secret = ICP.get_param('mint.inventory_service_secret') or ''
    if not base_url:
        _logger.info('mint.inventory_service_url not set; skipping Dutchie enqueue')
        return

    def _send():
        url = f"{base_url}/jobs/dutchie-customer-create"
        body = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(
            url, data=body, method='POST',
            headers={
                'Content-Type': 'application/json',
                'X-Mint-Secret': secret,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                resp.read()
        except (urllib.error.URLError, urllib.error.HTTPError) as e:
            _logger.warning('Dutchie enqueue failed for partner %s: %s',
                            payload.get('partner_id'), e)
        except Exception:
            _logger.exception('Unexpected error enqueueing Dutchie create')

    threading.Thread(target=_send, daemon=True).start()


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

        try:
            data = json.loads(request.httprequest.data)
        except (json.JSONDecodeError, TypeError):
            return error_response('Invalid JSON body')

        name = data.get('name', '').strip()
        email = data.get('email', '').strip().lower()
        password = data.get('password', '')
        phone = data.get('phone', '').strip()

        # Optional scan-v2 fields — if a US state ID was scanned on the
        # /register page, these carry the parsed AAMVA record. None of them
        # are required; when absent we fall back to the legacy name-only
        # signup.
        first_name = (data.get('first_name') or '').strip()
        last_name = (data.get('last_name') or '').strip()
        date_of_birth = (data.get('date_of_birth') or '').strip() or False
        dl_number = (data.get('dl_number') or '').strip() or False
        dl_state = (data.get('dl_state') or '').strip().upper()[:2] or False
        dl_expiration = (data.get('dl_expiration') or '').strip() or False
        address_street = (data.get('address_street') or '').strip()
        address_city = (data.get('address_city') or '').strip()
        address_state = (data.get('address_state') or '').strip().upper()[:2]
        address_zip = (data.get('address_zip') or '').strip()

        # Store selection — selectedStore.id (res.company id) and
        # selectedStore.DutchieStoreID (char) carried from localStorage.
        try:
            home_store_id = int(data.get('home_store_id') or 0) or False
        except (TypeError, ValueError):
            home_store_id = False
        dutchie_location_id = (data.get('dutchie_location_id') or '').strip() or False

        if not name or not email or not password:
            return error_response('Name, email, and password are required')

        if len(password) < 8:
            return error_response('Password must be at least 8 characters')

        # Only block on existing WEB customer account. An internal employee
        # with login=<email> (share=False) must still be able to sign up as
        # a separate customer (their work identity stays disjoint).
        existing = request.env['res.users'].sudo().search(
            [('login', '=', _web_login(email))], limit=1
        )
        if existing:
            return error_response('An account with this email already exists', 409)

        # Resolve address state_id via USPS 2-letter code lookup. Keeps US-only
        # for now — the scanner emits USPS codes.
        state_id = False
        if address_state:
            us = request.env['res.country'].sudo().search(
                [('code', '=', 'US')], limit=1
            )
            if us:
                state = request.env['res.country.state'].sudo().search(
                    [('country_id', '=', us.id), ('code', '=', address_state)],
                    limit=1,
                )
                state_id = state.id if state else False

        try:
            user = self._create_web_user(email=email, name=name, phone=phone)
            user.with_context(
                no_reset_password=True,
                tracking_disable=True,
            ).write({'password': password})

            token = user._generate_jwt()

            # Fire-and-forget Dutchie customer create. Signup response is not
            # blocked by this — if the enqueue or the downstream job fails,
            # the nightly Dutchie sync will eventually reconcile the customer.
            if dutchie_location_id or home_store_id:
                _enqueue_dutchie_create({
                    'partner_id': partner.id,
                    'first_name': first_name or name.split(' ', 1)[0],
                    'last_name': last_name or (name.split(' ', 1)[1] if ' ' in name else ''),
                    'email': email,
                    'phone': phone or '',
                    'date_of_birth': date_of_birth or '',
                    'dl_number': dl_number or '',
                    'dl_state': dl_state or '',
                    'dl_expiration': dl_expiration or '',
                    'address_street': address_street,
                    'address_city': address_city,
                    'address_state': address_state,
                    'address_zip': address_zip,
                    'dutchie_location_id': dutchie_location_id or '',
                    'home_store_id': home_store_id or None,
                })
                dutchie_status = 'pending'
            else:
                dutchie_status = 'skipped'

            return json_response({
                'token': token,
                'user': {
                    'id': user.id,
                    'name': user.partner_id.name,
                    'email': user.partner_id.email or user.login.removeprefix(WEB_LOGIN_PREFIX),
                    'phone': user.partner_id.phone or '',
                    'partner_id': user.partner_id.id,
                    'home_store_id': user.partner_id.x_home_store_id.id or None,
                    'home_store_source': user.partner_id.x_home_store_source or None,
                },
                'status': {
                    'odoo': 'ok',
                    'dutchie': dutchie_status,
                    'synced': False,
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
        """Generate a reset token and email a FE-hosted reset link.

        We don't call user.action_reset_password() because that emails
        Odoo's built-in /web/reset_password page. Instead we generate the
        signup token manually and send a custom email pointing at
        {frontend_url}/account/reset-password?token=...

        Set ir.config_parameter 'mint.frontend_url' (default
        https://letsgomint.us) to change where the link points — useful
        for staging.
        """
        if request.httprequest.method == 'OPTIONS':
            return json_response({})

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
            if user and user.partner_id:
                self._send_reset_email(user)
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

        api_key = request.httprequest.headers.get('X-Api-Key', '')
        if not api_key or not self._verify_fe_api_key(api_key):
            return error_response('Unauthorized', 401)

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

    @http.route('/api/v1/auth/verify-internal', type='http', auth='none',
                methods=['POST', 'OPTIONS'], csrf=False, cors='*')
    def verify_internal(self, **kw):
        """Confirm an email maps to an ACTIVE INTERNAL Odoo user (share=False).

        Used by the Mint Tools Chrome extension to gate the side panel to
        internal staff only. Called server-to-server by the Astro edge
        endpoint AFTER it has verified the Google id_token; trust on the Odoo
        side relies on X-Api-Key (only the frontend server holds it). This is
        the inverse of google_auth: here we REQUIRE share=False and never
        create or mutate anything.
        """
        if request.httprequest.method == 'OPTIONS':
            return json_response({})

        api_key = request.httprequest.headers.get('X-Api-Key', '')
        if not api_key or not self._verify_fe_api_key(api_key):
            return error_response('Unauthorized', 401)

        try:
            data = json.loads(request.httprequest.data)
        except (json.JSONDecodeError, TypeError):
            return error_response('Invalid JSON body')

        email = (data.get('email') or '').strip().lower()
        if not email:
            return error_response('email is required')

        # Internal employees have login = plain email and share=False. The
        # web-customer accounts (share=True, login='web:<email>') are
        # explicitly excluded, so a customer Google account can never unlock
        # the extension.
        user = request.env['res.users'].sudo().search(
            [
                ('login', '=', email),
                ('share', '=', False),
                ('active', '=', True),
            ],
            limit=1,
        )
        if not user:
            return json_response({'ok': False}, status=403)

        return json_response({
            'ok': True,
            'user': {
                'id': user.id,
                'name': user.partner_id.name or user.name,
                'login': user.login,
                'email': user.partner_id.email or user.login,
            },
        })

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

        partner = user.partner_id
        return json_response({
            'user': {
                'id': user.id,
                'name': partner.name,
                'email': user.login,
                'partner_id': partner.id,
                'home_store_id': partner.x_home_store_id.id or None,
                'home_store_source': partner.x_home_store_source or None,
                'dutchie_customer_id': partner.x_dutchie_customer_id or None,
            },
        })

    @http.route('/api/v1/auth/signup-status', type='http', auth='none',
                methods=['GET', 'OPTIONS'], csrf=False, cors='*')
    def signup_status(self, **kw):
        """Return the three-way sync status for the signed-in user.

        Used by the frontend to poll after signup — shows a "Dutchie account
        linking..." banner until dutchie + synced both flip to true.

        Status semantics:
          odoo   — always 'ok' here (JWT proves the Odoo user exists)
          dutchie — 'pending' (no customer_id yet and no failure recorded),
                    'ok' (x_dutchie_customer_id is set),
                    'failed' (job exhausted retries — signalled by
                    x_dutchie_sync_error, if the BullMQ worker has writeback
                    that field later),
                    'skipped' (signup happened without a store context)
          synced — true when dutchie == 'ok' AND the Odoo partner carries the
                   Dutchie customer id.
        """
        if request.httprequest.method == 'OPTIONS':
            return json_response({})

        user = _verify_and_get_user()
        if not user:
            return error_response('Invalid or expired token', 401)

        partner = user.partner_id
        dutchie_id = partner.x_dutchie_customer_id or ''
        if dutchie_id:
            dutchie_state = 'ok'
            synced = True
        elif not partner.x_dutchie_store_id and not partner.x_home_store_id:
            dutchie_state = 'skipped'
            synced = False
        else:
            dutchie_state = 'pending'
            synced = False

        return json_response({
            'odoo': 'ok',
            'dutchie': dutchie_state,
            'synced': synced,
            'dutchie_customer_id': dutchie_id or None,
        })

    @http.route('/api/v1/auth/reset-password', type='http', auth='none',
                methods=['POST', 'OPTIONS'], csrf=False, cors='*')
    def reset_password(self, **kw):
        """Consume the signup token generated by /auth/forgot-password and
        set a new password. Token comes from the reset-password email, which
        now links to letsgomint.us/account/reset-password?token=... — the FE
        posts here with {token, new_password}.

        Returns a fresh JWT on success so the user is signed in immediately."""
        if request.httprequest.method == 'OPTIONS':
            return json_response({})

        try:
            data = json.loads(request.httprequest.data)
        except (json.JSONDecodeError, TypeError):
            return error_response('Invalid JSON body')

        token = (data.get('token') or '').strip()
        new_password = data.get('new_password') or ''
        if not token:
            return error_response('Token is required')
        if len(new_password) < 8:
            return error_response('Password must be at least 8 characters')

        # signup_retrieve_info validates the token's existence + expiration.
        # Raises SignupError with a user-facing message if invalid/expired.
        try:
            info = request.env['res.users'].sudo().signup_retrieve_info(token)
        except Exception as e:
            # Don't leak whether the token ever existed vs. just expired.
            _logger.info('reset-password token rejected: %s', e)
            return error_response('Invalid or expired reset token', 400)

        login = info.get('login')
        user = request.env['res.users'].sudo().search(
            [('login', '=', login)], limit=1,
        )
        if not user:
            return error_response('Invalid or expired reset token', 400)

        try:
            user._update_password(new_password)
            # Invalidate the consumed token so it can't be reused.
            user.partner_id.sudo().write({
                'signup_token': False,
                'signup_type': False,
                'signup_expiration': False,
            })
        except Exception as e:
            _logger.exception('reset-password update failed for %s', login)
            return error_response(f'Could not update password: {e}', 500)

        fresh_token = user._generate_jwt()
        return json_response({
            'token': fresh_token,
            'user': {
                'id': user.id,
                'name': user.partner_id.name,
                'email': user.partner_id.email or user.login.removeprefix(WEB_LOGIN_PREFIX),
                'phone': user.partner_id.phone or '',
                'partner_id': user.partner_id.id,
            },
        })
