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

        # Check if user already exists
        existing = request.env['res.users'].sudo().search(
            [('login', '=', email)], limit=1
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
            # Create user via raw SQL + ORM hybrid (Odoo 19 create hooks
            # cause transaction rollbacks due to mail/signup side effects)
            main_company = request.env['res.company'].sudo().browse(1)

            partner_vals = {
                'name': name,
                'email': email,
                'phone': phone or False,
                'customer_rank': 1,
                'company_id': main_company.id,
            }
            if address_street:
                partner_vals['street'] = address_street
            if address_city:
                partner_vals['city'] = address_city
            if state_id:
                partner_vals['state_id'] = state_id
            if address_zip:
                partner_vals['zip'] = address_zip
            if home_store_id:
                partner_vals['x_home_store_id'] = home_store_id
                partner_vals['x_home_store_source'] = 'portal'
            if dutchie_location_id:
                partner_vals['x_dutchie_store_id'] = dutchie_location_id
            if dl_number:
                partner_vals['x_dl_number'] = dl_number
            if dl_state:
                partner_vals['x_dl_state'] = dl_state
            if dl_expiration:
                partner_vals['x_dl_expiration'] = dl_expiration
            if dl_number or date_of_birth:
                partner_vals['x_dl_scanned_at'] = datetime.utcnow()

            # Create partner first
            partner = request.env['res.partner'].sudo().with_context(
                mail_create_nosubscribe=True,
                mail_create_nolog=True,
                tracking_disable=True,
                mail_notrack=True,
            ).create(partner_vals)
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
                    'email': user.login,
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

        # Always return success to prevent email enumeration — timing is the
        # only remaining signal, which is acceptable for this threat model.
        try:
            user = request.env['res.users'].sudo().search(
                [('login', '=', email)], limit=1
            )
            if user and user.partner_id:
                self._send_reset_email(user)
        except Exception:
            _logger.exception('Password reset failed for %s', email)

        return json_response({
            'message': 'If an account exists with that email, a reset link has been sent.',
        })

    def _send_reset_email(self, user):
        """Generate a reset token and dispatch a mint-branded email."""
        partner = user.partner_id
        # signup_prepare generates a token + expiration + sets signup_type='reset'
        partner.sudo().signup_prepare(signup_type='reset', expiration=False)
        partner.invalidate_recordset(['signup_token', 'signup_expiration'])

        token = partner.signup_token
        if not token:
            _logger.warning('signup_prepare did not yield a token for partner=%s', partner.id)
            return

        ICP = request.env['ir.config_parameter'].sudo()
        frontend_url = ICP.get_param('mint.frontend_url', 'https://letsgomint.us').rstrip('/')
        reset_url = f"{frontend_url}/account/reset-password?token={token}"

        first_name = (partner.name or '').split(' ')[0] or 'there'
        subject = "Reset your Mint password"
        body_html = f"""
            <p>Hi {first_name},</p>
            <p>We got a request to reset the password for your Mint account
               ({user.login}). Click the link below to choose a new one:</p>
            <p><a href="{reset_url}"
                  style="background:#2d7d3a;color:#fff;padding:12px 20px;
                         border-radius:6px;text-decoration:none;
                         display:inline-block;">Reset my password</a></p>
            <p>This link expires in 24 hours. If you didn't request this,
               you can safely ignore the email.</p>
            <p>— The Mint team</p>
        """

        request.env['mail.mail'].sudo().create({
            'subject': subject,
            'body_html': body_html,
            'email_to': user.login,
            'auto_delete': True,
        }).send()

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
                'email': user.login,
                'partner_id': user.partner_id.id,
            },
        })
