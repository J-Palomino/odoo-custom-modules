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
    Returns None when database.secret is unset so callers fail closed with a
    clean error / 404 rather than minting a forgeable empty-key token.
    """
    secret = (request.env['ir.config_parameter'].sudo()
              .get_param('database.secret', '') or '')
    if not secret:
        _logger.error('database.secret unavailable; cannot issue/verify lead token')
        return None
    return hmac.new(secret.encode('utf-8'), str(lead_id).encode('utf-8'),
                    hashlib.sha256).hexdigest()


def _clean_token(v):
    """Coerce a client-supplied token to a stripped str; '' for non-strings.
    Guards against a JSON number/list/bool reaching .strip() (AttributeError)."""
    return v.strip() if isinstance(v, str) else ''


def _token_eq(a, b):
    """Constant-time token compare, tolerant of non-ASCII input.
    (hmac.compare_digest raises TypeError on a non-ASCII str, so compare bytes.)
    Returns False on any falsy operand or error — never raises."""
    if not a or not b:
        return False
    try:
        return hmac.compare_digest(a.encode('utf-8'), b.encode('utf-8'))
    except Exception:
        return False


def _enqueue_dutchie_create(payload):
    """Best-effort, non-blocking POST to create the Dutchie guest (#101243).

    A signup must never fail or stall because of a Dutchie hiccup, so the
    network call runs on a daemon thread (touching NO Odoo env — env is not
    thread-safe — only captured plain values). The nightly Dutchie sync
    reconciles the customer either way. Sends only non-sensitive fields.
    """
    import threading
    import urllib.request
    ICP = request.env['ir.config_parameter'].sudo()
    base = (ICP.get_param('mint.inventory_service_url', '') or '').rstrip('/')
    if not base:
        _logger.info('mint.inventory_service_url not set; skipping Dutchie enqueue')
        return
    api_key = ICP.get_param('mint.inventory_service.api_key', '') or ''
    url = '%s/jobs/dutchie-customer-create' % base
    body = json.dumps(payload).encode('utf-8')

    def _post():
        try:
            req = urllib.request.Request(
                url, data=body,
                headers={'Content-Type': 'application/json', 'x-api-key': api_key},
                method='POST',
            )
            urllib.request.urlopen(req, timeout=5)
        except Exception as e:
            _logger.warning('Dutchie enqueue failed for partner %s: %s',
                            payload.get('partner_id'), e)

    threading.Thread(target=_post, name='dutchie-enqueue', daemon=True).start()


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
            # if the caller proves ownership of a pre-account lead via its HMAC
            # token, delete it here too — the DOB just revealed a minor, and the
            # CRM must hold no minor PII. Best-effort; the 403 stands either way.
            try:
                u21_lead_id = int(data.get('lead_id') or 0) or False
                u21_token = _clean_token(data.get('lead_token'))
                if u21_lead_id and u21_token and _token_eq(u21_token, _lead_token(u21_lead_id)):
                    request.env['crm.lead'].sudo().with_context(
                        tracking_disable=True, mail_notrack=True
                    ).browse(u21_lead_id).exists().unlink()
            except Exception as e:
                _logger.warning('Under-21 lead cleanup failed (lead %s): %s',
                                data.get('lead_id'), e)
            return error_response('You must be 21 or older to create an account', 403)

        # Only block on existing WEB customer account. An internal employee
        # with login=<email> (share=False) must still be able to sign up as
        # a separate customer (their work identity stays disjoint).
        existing = request.env['res.users'].sudo().search(
            [('login', '=', _web_login(email))], limit=1
        )
        if existing:
            return error_response('An account with this email already exists', 409)

        # Age-verification method. The web DOB is self-attested. We deliberately
        # do NOT honor a client-supplied 'id_scanned' claim: nothing server-side
        # has validated that an ID document was actually scanned, so trusting the
        # request body would overstate rigor in what is meant to be a provable
        # compliance record. Until the IdScanner backend issues a server-
        # verifiable scan token (then verify it here before upgrading the
        # method), every web signup is recorded as self_attested.
        method = 'self_attested'

        try:
            user = self._create_web_user(
                email=email, name=name, phone=phone,
                web_dob=dob, age_method=method, age_source='web_register',
            )
            user.with_context(
                no_reset_password=True,
                tracking_disable=True,
            ).write({'password': password})

            # Provable marketing consent (opt-in, OFF by default). Reuses the
            # canonical consent ledgers: set_email_opt_in (mint_account) and
            # set_sms_opt_in (mint_sms_telnyx), both of which stamp date +
            # source. Defensive hasattr so signup never breaks if a consent
            # module is absent; wrapped so a consent error can't fail the
            # account creation. Transactional email/SMS is exempt and unaffected.
            def _opted_in(v):
                # Strict coercion: a provable consent record must not opt a
                # user in on a stringy "false"/"0" that is merely truthy.
                return v is True or str(v).strip().lower() in ('true', '1', 'yes', 'on')

            try:
                partner = user.partner_id.sudo()
                if _opted_in(data.get('emailOptIn')) and hasattr(partner, 'set_email_opt_in'):
                    partner.set_email_opt_in(source='external_web')
                if _opted_in(data.get('smsOptIn')) and hasattr(partner, 'set_sms_opt_in'):
                    partner.set_sms_opt_in(source='external_web')
            except Exception:
                _logger.exception('Consent capture failed for %s', email)

            # Link the pre-account visitor lead to this new contact (#101243).
            # HMAC-bound so a registrant can't repoint an arbitrary lead's
            # partner_id (IDOR). Isolated: the account is already committed here,
            # so a crm.lead hook failure must NOT turn a successful signup into a
            # 500 (which would then lock the user out via the 409 guard on retry).
            try:
                lead_id = int(data.get('lead_id') or 0) or False
            except (TypeError, ValueError):
                lead_id = False
            lead_token = _clean_token(data.get('lead_token'))
            if lead_id and lead_token:
                try:
                    # partner_id is TRACKED on crm.lead — without mail-hook
                    # suppression this write crashes in the user-less env and
                    # the except would silently drop the link EVERY time.
                    lead = request.env['crm.lead'].sudo().with_context(
                        tracking_disable=True, mail_notrack=True).browse(lead_id)
                    if lead.exists() and _token_eq(lead_token, _lead_token(lead_id)):
                        lead.write({'partner_id': user.partner_id.id,
                                    'date_conversion': fields.Datetime.now()})
                except Exception as e:
                    _logger.warning('Lead link failed (lead %s -> partner %s): %s',
                                    lead_id, user.partner_id.id, e)

            token = user._generate_jwt()

            # Dutchie guest create — best-effort, non-blocking, fired last.
            # Guarded so a serialize/config hiccup here can't 500 an already-
            # committed signup (which would then 409-lock the user on retry).
            try:
                _enqueue_dutchie_create({
                    'partner_id': user.partner_id.id,
                    'name': name,
                    'email': email,
                    'phone': phone,
                    'date_of_birth': dob.isoformat(),
                })
            except Exception as e:
                _logger.warning('Dutchie enqueue dispatch failed for partner %s: %s',
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
    # res.partner contact on register() once the account is created.
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
        # Verify we can issue a token BEFORE creating the lead, so a missing
        # database.secret doesn't leave an orphaned, unconvertible lead behind.
        if _lead_token(0) is None:
            return error_response('Unable to issue lead token', 503)
        ip = request.httprequest.headers.get(
            'X-Forwarded-For', request.httprequest.remote_addr or '')
        ua = request.httprequest.headers.get('User-Agent', '')[:200]
        # Guard the cross-module crm.lead create: a required field, automation,
        # or record rule from another crm-extending module must return a clean
        # JSON error, not an uncaught 500.
        try:
            # auth='none' → request.env.user is an EMPTY recordset; crm.lead is a
            # mail.thread whose create() posts a chatter log that calls
            # env.user._is_public() → "Expected singleton: res.users()" crash
            # (hit live on staging). Suppress the mail hooks like
            # _create_web_user does — same house pattern.
            lead = request.env['crm.lead'].sudo().with_context(
                mail_create_nosubscribe=True,
                mail_create_nolog=True,
                tracking_disable=True,
                mail_notrack=True,
            ).create({
                'name': 'Web signup: %s' % email,
                'contact_name': (data.get('name') or '').strip() or False,
                'email_from': email,
                'phone': (data.get('phone') or '').strip() or False,
                'type': 'lead',
                'description': ('Web onboarding lead. age_verified=false. '
                               'consent_proof: ts=%s source=external_web ip=%s ua=%s'
                               % (fields.Datetime.now(), ip, ua)),
            })
        except Exception as e:
            _logger.exception('create_lead failed for %s: %s', email, e)
            return error_response('Could not create lead', 500)
        tok = _lead_token(lead.id)
        if not tok:
            return error_response('Unable to issue lead token', 503)
        return json_response(
            {'lead_id': lead.id, 'lead_token': tok}, status=201)

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
        # tracking_disable/mail_notrack: lost_reason_id is a TRACKED field —
        # writing it from this user-less (auth='none') env would fire the same
        # singleton message_post crash as create (see create_lead).
        lead = request.env['crm.lead'].sudo().with_context(
            tracking_disable=True, mail_notrack=True).browse(lead_id)
        # Ownership binding: the caller must present the server-issued token from
        # create_lead (HMAC of the id). 404 (not 403) on mismatch/missing-secret
        # so the endpoint neither confirms which ids exist nor leaks misconfig.
        token = _clean_token(data.get('lead_token'))
        if not lead.exists() or not _token_eq(token, _lead_token(lead_id)):
            return error_response('Lead not found', 404)
        from datetime import date
        dob_raw = str(data.get('dateOfBirth') or data.get('date_of_birth') or '').strip()
        try:
            dob = date.fromisoformat(dob_raw[:10])
        except (ValueError, TypeError):
            return error_response('Invalid date of birth')
        today = date.today()
        age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
        # Guard the crm.lead / crm.lost.reason writes: a cross-module hook, rule,
        # or automation must not turn a valid verification into an uncaught 500.
        try:
            if age < 21:
                # NEVER retain an under-21 lead (owner decision 2026-07-02,
                # supersedes the earlier archive+lost-reason design): holding a
                # minor's PII in the marketing CRM is a compliance liability, so
                # the record is deleted outright — not archived.
                lead.unlink()
                return error_response('You must be 21 or older', 403)
            lead.write({'description': (lead.description or '')
                        + '\nage_verified=true dob=%s' % dob.isoformat()})
        except Exception as e:
            _logger.exception('verify_lead write failed for lead %s: %s', lead_id, e)
            return error_response('Could not record verification', 500)
        return json_response({'ok': True, 'age_verified': True})

    def _create_web_user(self, email, name, phone='', web_dob=None,
                         age_method=None, age_source=None):
        """Create a fresh partner + portal user for a web-site signup.

        Always creates a NEW res.partner (doesn't reuse an existing one that
        happens to have the same email) so employees shopping the consumer
        site get a separate customer identity from their staff partner.

        Uses login = 'web:<email>' so web users never collide with internal
        user accounts (login = plain email) and sets is_web_customer=True
        so record rules restrict PII to privileged groups.

        When ``web_dob`` (a datetime.date) is supplied, the partner is stamped
        with the web-side age-verification ledger fields so the 21+ check is
        provable and linkable to Dutchie. Callers without a DOB (e.g. Google
        OAuth) leave the account age_verified=False on purpose.
        """
        main_company = request.env['res.company'].sudo().browse(1)
        login = _web_login(email)

        partner_vals = {
            'name': name,
            'email': email,
            'phone': phone or False,
            'customer_rank': 1,
            'company_id': main_company.id,
            'is_web_customer': True,
        }
        if web_dob:
            partner_vals.update({
                'web_date_of_birth': web_dob,
                'age_verified': True,
                'age_verified_at': fields.Datetime.now(),
                'age_verification_method': age_method or 'self_attested',
                'age_verification_source': age_source or 'web_register',
            })

        partner = request.env['res.partner'].sudo().with_context(
            mail_create_nosubscribe=True,
            mail_create_nolog=True,
            tracking_disable=True,
            mail_notrack=True,
        ).create(partner_vals)
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
