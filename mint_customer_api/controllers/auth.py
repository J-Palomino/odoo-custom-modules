# -*- coding: utf-8 -*-
"""
Customer authentication endpoints for MintDeals frontend.

All endpoints return JSON, use auth='none' since we handle auth via JWT.
"""
import hashlib
import hmac
import json
import logging

from odoo import http, fields
from odoo.http import request, Response
from odoo.exceptions import AccessDenied, UserError

_logger = logging.getLogger(__name__)


def _lead_token(lead_id):
    """Server-issued capability token binding a caller to a specific crm.lead.

    HMAC of the lead id under the instance's database.secret — unguessable, so
    holding a lead_id alone (or knowing the signup email) is NOT enough to verify
    or convert it; the caller must present the token returned by create_lead.
    No new schema needed.
    """
    secret = (request.env['ir.config_parameter'].sudo()
              .get_param('database.secret', '') or '')
    return hmac.new(secret.encode('utf-8'), str(lead_id).encode('utf-8'),
                    hashlib.sha256).hexdigest()


def _enqueue_dutchie_create(payload):
    """Fire-and-forget POST to the inventory service to create the Dutchie guest.

    Best-effort: a signup must never fail because of a Dutchie hiccup; the
    nightly Dutchie sync reconciles the customer either way. Sends only
    non-sensitive fields — NOT the driver's-license number (PII; Odoo #101243).
    """
    import urllib.request
    ICP = request.env['ir.config_parameter'].sudo()
    base = (ICP.get_param('mint.inventory_service_url', '') or '').rstrip('/')
    if not base:
        _logger.info('mint.inventory_service_url not set; skipping Dutchie enqueue')
        return
    api_key = ICP.get_param('mint.inventory_service.api_key', '') or ''
    try:
        req = urllib.request.Request(
            '%s/jobs/dutchie-customer-create' % base,
            data=json.dumps(payload).encode('utf-8'),
            headers={'Content-Type': 'application/json', 'x-api-key': api_key},
            method='POST',
        )
        urllib.request.urlopen(req, timeout=3)
    except Exception as e:
        _logger.warning('Dutchie enqueue failed for partner %s: %s',
                        payload.get('partner_id'), e)

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
            # Under-21 leads are NEVER retained (owner decision 2026-07-02):
            # delete the caller's pre-account lead when ownership is proven via
            # its HMAC token. Best-effort; the 403 stands either way.
            try:
                u21_lead_id = int(data.get('lead_id') or 0) or False
                u21_token = (str(data.get('lead_token') or '')).strip()
                if u21_lead_id and u21_token and hmac.compare_digest(
                        u21_token.encode('utf-8'),
                        (_lead_token(u21_lead_id) or '').encode('utf-8')):
                    request.env['crm.lead'].sudo().with_context(
                        tracking_disable=True, mail_notrack=True
                    ).browse(u21_lead_id).exists().unlink()
            except Exception as e:
                _logger.warning('Under-21 lead cleanup failed (lead %s): %s',
                                data.get('lead_id'), e)
            return error_response('You must be 21 or older to create an account', 403)

        # --- Per-channel consent + employee gating (Odoo #101243) ---
        # Employees (by configurable email domain) must opt in to all three
        # channels; everyone else to at least one. Gate on PERSISTABLE consents
        # (field must exist on this instance) so we never return 201 for a signup
        # whose requested consent couldn't actually be recorded (silent-drop bug).
        # Enforced server-side so a manipulated client can't bypass it.
        email_opt = bool(data.get('email_opt_in'))
        call_opt = bool(data.get('call_opt_in'))
        sms_opt = bool(data.get('sms_opt_in'))
        pf = request.env['res.partner']._fields
        email_can = 'email_opt_in' in pf
        call_can = 'call_opt_in' in pf
        sms_can = 'sms_opt_in' in pf
        ICP = request.env['ir.config_parameter'].sudo()
        emp_domains = [x.strip().lower() for x in
                       (ICP.get_param('mint.employee_email_domains', '') or '').split(',')
                       if x.strip()]
        domain = email.split('@')[-1] if '@' in email else ''
        is_employee = domain in emp_domains
        persistable = [email_opt and email_can, call_opt and call_can, sms_opt and sms_can]
        if is_employee:
            if not (email_opt and call_opt and sms_opt):
                return error_response(
                    'Employees must opt in to text, call, and email to create an account', 403)
            if not (email_can and call_can and sms_can):
                return error_response(
                    'Consent channels are not all available on this environment yet', 503)
        elif not any(persistable):
            return error_response(
                'Please opt in to at least one available channel (text, call, or email)', 400)

        # Build the consent + 21+ partner values. opt_in source must be a valid
        # selection — 'external_web' (Customer Web Form). DOB + age_verified are
        # the only ID-scan PII we persist (no license number/image; decision #3).
        now = fields.Datetime.now()
        SRC = 'external_web'
        # Write only fields that exist on this instance. The x_ fields
        # (mint_dutchie_sync) and email/call consent (mint_account, deal-chatter,
        # not yet on staging) may be absent depending on deploy state, so every
        # write is field-guarded to avoid an -u/runtime crash on a missing column.
        extra_vals = {}
        if 'x_date_of_birth' in pf:
            extra_vals['x_date_of_birth'] = dob
        if 'x_age_verified' in pf:
            extra_vals['x_age_verified'] = True

        def _consent_vals(channel):
            # Build opt-in vals, including only columns that actually exist — a
            # partial mint_account merge could define <chan>_opt_in without its
            # _date/_source companions; guard each individually.
            out = {}
            for suffix, val in (('', True), ('_date', now), ('_source', SRC)):
                f = '%s_opt_in%s' % (channel, suffix)
                if f in pf:
                    out[f] = val
            return out
        if email_opt:
            extra_vals.update(_consent_vals('email'))
        if call_opt:
            extra_vals.update(_consent_vals('call'))
        # SMS consent is granted post-create via set_sms_opt_in() so the partner
        # also gets the SMS whitelist category tag — writing sms_opt_in directly
        # would skip the tag and every text would be blocked by _check_sendable().
        home_store_id = int(data.get('home_store_id') or 0) or False
        if home_store_id and 'x_home_store_id' in pf:
            extra_vals['x_home_store_id'] = home_store_id

        # Only block on existing WEB customer account. An internal employee
        # with login=<email> (share=False) must still be able to sign up as
        # a separate customer (their work identity stays disjoint).
        existing = request.env['res.users'].sudo().search(
            [('login', '=', _web_login(email))], limit=1
        )
        if existing:
            return error_response('An account with this email already exists', 409)

        try:
            user = self._create_web_user(
                email=email, name=name, phone=phone, extra_vals=extra_vals)
            user.with_context(
                no_reset_password=True,
                tracking_disable=True,
            ).write({'password': password})

            # SMS consent via the helper so the partner also gets the whitelist
            # category tag (a direct sms_opt_in write would skip it → texts blocked).
            # Isolated try + tracking_disable: a whitelist/category hiccup must
            # degrade to "no SMS consent", never roll back the whole signup.
            if sms_opt and sms_can:
                try:
                    user.partner_id.with_context(
                        tracking_disable=True, mail_notrack=True
                    ).set_sms_opt_in(source=SRC)
                except Exception as e:
                    _logger.warning('set_sms_opt_in failed for partner %s: %s',
                                    user.partner_id.id, e)

            # Link the pre-account visitor lead to this new contact (#101243) —
            # ONLY when the lead belongs to this email, so a registrant can't
            # repoint an arbitrary lead's partner_id (IDOR).
            lead_id = int(data.get('lead_id') or 0) or False
            lead_token = (data.get('lead_token') or '').strip()
            if lead_id and lead_token:
                lead = request.env['crm.lead'].sudo().with_context(
            tracking_disable=True, mail_notrack=True).browse(lead_id)
                if lead.exists() and hmac.compare_digest(lead_token, _lead_token(lead_id)):
                    lead.write({'partner_id': user.partner_id.id, 'date_conversion': now})

            token = user._generate_jwt()

            # Dutchie customer create — best-effort, fired LAST (after all
            # fallible work) to shrink the window where it POSTs for a signup
            # that then rolls back. Short timeout; never raises (see helper).
            _enqueue_dutchie_create({
                'partner_id': user.partner_id.id,
                'name': name,
                'email': email,
                'phone': phone,
                'date_of_birth': dob.isoformat(),
                'home_store_id': home_store_id or None,
                'dutchie_location_id': (data.get('dutchie_location_id') or '').strip() or None,
            })

            # Welcome free pre-roll — issue the customer's unique, single-use
            # coupon at signup rather than waiting up to 30 min for the sweep
            # cron. _issue_welcome_preroll is idempotent and self-gates on the
            # mint.welcome_preroll config, and the cron stays as a backstop.
            # Best-effort + guarded: unlike _enqueue_dutchie_create (which never
            # raises), this does config reads + Dutchie pushes, so a hiccup here
            # must not 500 an already-committed signup (→ 409-lock on retry).
            try:
                request.env['mint.discount'].sudo()._issue_welcome_preroll(
                    user.partner_id.sudo())
            except Exception as e:
                _logger.warning('Welcome pre-roll issue failed for partner %s: %s',
                                user.partner_id.id, e)

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
    @http.route('/api/v1/auth/apple', type='http', auth='none',
                methods=['POST', 'OPTIONS'], csrf=False, cors='*')
    def apple_auth(self, **kw):
        """Sign-in with Apple. Called server-to-server by the Astro callback
        after it exchanges the OAuth code with Apple and verifies the id_token
        against Apple's JWKS. Mirrors google_auth: trust relies on X-Api-Key
        (only the frontend server holds it). Find-or-create is keyed on email
        exactly like Google; Apple's private-relay email is stable per Services
        ID, so repeat logins resolve to the same account. Controller-only — no
        model change, so deploys with a plain restart (no -u)."""
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
        apple_sub = (data.get('apple_sub') or '').strip()

        if not email or not apple_sub:
            return error_response('email and apple_sub are required')

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
                _logger.exception('Apple OAuth account creation failed: %s', e)
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

    # ------------------------------------------------------------------
    # Visitor leads (Odoo #101243) — created pre-account, converted to a
    # res.partner contact on register() once consent is granted.
    # ------------------------------------------------------------------
    @http.route('/api/v1/leads', type='http', auth='none',
                methods=['POST', 'OPTIONS'], csrf=False, cors='*')
    def create_lead(self, **kw):
        """Create a pre-account visitor lead. Called right after Google auth,
        before the account exists. Captures provable-consent metadata."""
        if request.httprequest.method == 'OPTIONS':
            return json_response({})
        gate = self._require_fe_key()
        if gate:
            return gate
        try:
            data = json.loads(request.httprequest.data)
        except (json.JSONDecodeError, TypeError):
            return error_response('Invalid JSON body')
        email = (data.get('email') or '').strip().lower()
        if not email:
            return error_response('Email is required')
        ip = request.httprequest.headers.get(
            'X-Forwarded-For', request.httprequest.remote_addr or '')
        ua = request.httprequest.headers.get('User-Agent', '')[:200]
        lead = request.env['crm.lead'].sudo().with_context(
            mail_create_nosubscribe=True, mail_create_nolog=True,
            tracking_disable=True, mail_notrack=True).create({
            'name': 'Web signup: %s' % email,
            'contact_name': (data.get('name') or '').strip() or False,
            'email_from': email,
            'phone': (data.get('phone') or '').strip() or False,
            'type': 'lead',
            'description': ('Web onboarding lead. age_verified=false. '
                           'consent_proof: ts=%s source=external_web ip=%s ua=%s'
                           % (fields.Datetime.now(), ip, ua)),
        })
        return json_response(
            {'lead_id': lead.id, 'lead_token': _lead_token(lead.id)}, status=201)

    @http.route('/api/v1/leads/<int:lead_id>/verify', type='http', auth='none',
                methods=['POST', 'OPTIONS'], csrf=False, cors='*')
    def verify_lead(self, lead_id, **kw):
        """Attach 21+ verification to a lead after the ID scan. Under-21 → the
        lead is DELETED outright — under-21 PII is never retained."""
        if request.httprequest.method == 'OPTIONS':
            return json_response({})
        gate = self._require_fe_key()
        if gate:
            return gate
        try:
            data = json.loads(request.httprequest.data)
        except (json.JSONDecodeError, TypeError):
            return error_response('Invalid JSON body')
        lead = request.env['crm.lead'].sudo().with_context(
            tracking_disable=True, mail_notrack=True).browse(lead_id)
        # Ownership binding: the caller must present the server-issued token from
        # create_lead (HMAC of the id). email_from is guessable, so a token — not
        # the email — is the authorization boundary, closing the IDOR. 404 (not
        # 403) on mismatch so the endpoint doesn't confirm which ids exist.
        token = (data.get('lead_token') or '').strip()
        if not lead.exists() or not token or not hmac.compare_digest(token, _lead_token(lead_id)):
            return error_response('Lead not found', 404)
        from datetime import date
        dob_raw = (data.get('dateOfBirth') or data.get('date_of_birth') or '').strip()
        try:
            dob = date.fromisoformat(dob_raw[:10])
        except (ValueError, TypeError):
            return error_response('Invalid date of birth')
        today = date.today()
        age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
        if age < 21:
            # NEVER retain an under-21 lead (owner decision 2026-07-02,
            # supersedes archive+lost-reason): no minor PII in the CRM.
            lead.unlink()
            return error_response('You must be 21 or older', 403)
        lead.write({'description': (lead.description or '')
                    + '\nage_verified=true dob=%s' % dob.isoformat()})
        return json_response({'ok': True, 'age_verified': True})

    def _find_linkable_dutchie_partner(self, email, phone):
        """Find an existing Dutchie-linked customer partner to adopt for a web
        signup, so the new account inherits that customer's loyalty and
        purchase history instead of being orphaned. Returns a partner record
        or None.

        Resolves the same way checkout does (phone last-10 first, then email
        case-insensitively — mirrors checkout.py::_find_partner). Guarded so we
        never hijack a staff identity:

          * the partner must already be a Dutchie customer
            (``x_dutchie_customer_id`` set), and
          * it must not already back a login (no employee/web user) — an
            employee shopping the site keeps a disjoint customer identity.

        No-ops (returns None) if the Dutchie fields aren't installed, so the
        module still works without mint_api_v2.
        """
        Partner = request.env['res.partner'].sudo()
        if 'x_dutchie_customer_id' not in Partner._fields:
            return None

        base = [('x_dutchie_customer_id', '!=', False)]
        digits = ''.join(c for c in (phone or '') if c.isdigit())

        match = Partner.browse()
        if len(digits) >= 10:
            match = Partner.search(
                base + [('phone', 'ilike', digits[-10:])], order='id asc', limit=1)
        if not match and email:
            match = Partner.search(
                base + [('email', '=ilike', email)], order='id asc', limit=1)

        # Never adopt a partner that already backs a login (employee staff
        # account or an existing web customer) — keep identities disjoint.
        if not match or match.user_ids:
            return None
        return match

    def _create_web_user(self, email, name, phone='', extra_vals=None):
        """Create (or adopt a partner for) a portal user for a web-site signup.

        Adopts the customer's existing Dutchie-linked partner when one is found
        (see _find_linkable_dutchie_partner) so the account inherits their
        loyalty/history; otherwise creates a fresh res.partner. It never reuses
        an employee's staff partner, so employees shopping the consumer site
        keep a customer identity disjoint from their staff one.

        Uses login = 'web:<email>' so web users never collide with internal
        user accounts (login = plain email) and sets is_web_customer=True
        so record rules restrict PII to privileged groups.
        """
        main_company = request.env['res.company'].sudo().browse(1)
        login = _web_login(email)

        # Prefer adopting the customer's existing Dutchie-linked partner so the
        # web account inherits their loyalty and purchase history instead of
        # being orphaned as a fresh record (which is why signups previously
        # showed 0 points). Guarded to never claim an employee's staff partner
        # — see _find_linkable_dutchie_partner. None => create fresh (historical
        # behaviour).
        partner = self._find_linkable_dutchie_partner(email, phone)
        created_here = False
        if partner:
            adopt_vals = {'is_web_customer': True}
            if partner.customer_rank < 1:
                adopt_vals['customer_rank'] = 1
            if not partner.email:
                adopt_vals['email'] = email
            if phone and not partner.phone:
                adopt_vals['phone'] = phone
            # Fill only blank fields from extra_vals — never clobber the
            # customer's real Dutchie data (name, DOB, etc.).
            for key, val in (extra_vals or {}).items():
                if key in partner._fields and not partner[key]:
                    adopt_vals[key] = val
            partner.sudo().write(adopt_vals)
        else:
            partner_vals = {
                'name': name,
                'email': email,
                'phone': phone or False,
                'customer_rank': 1,
                'company_id': main_company.id,
                'is_web_customer': True,
            }
            if extra_vals:
                partner_vals.update(extra_vals)
            partner = request.env['res.partner'].sudo().with_context(
                mail_create_nosubscribe=True,
                mail_create_nolog=True,
                tracking_disable=True,
                mail_notrack=True,
            ).create(partner_vals)
            created_here = True
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
            # drop the extra partner we made to avoid orphans — but ONLY if we
            # created it here. Never unlink an adopted existing Dutchie partner.
            request.env.cr.execute(
                "SELECT id FROM res_users WHERE login = %s", (login,)
            )
            user_id = request.env.cr.fetchone()[0]
            if created_here:
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
