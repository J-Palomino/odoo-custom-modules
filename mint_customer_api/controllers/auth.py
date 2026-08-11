# -*- coding: utf-8 -*-
"""
Customer authentication endpoints for MintDeals frontend.

All endpoints return JSON, use auth='none' since we handle auth via JWT.
"""
import hashlib
import hmac
import json
import logging
import re
import time
import uuid

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


def _loyalty_points(partner):
    """Current Mint Rewards balance for a partner, 0 when there is no card.

    The PWA top bar (MobileTopBar.astro) reads `user.loyalty_points` off these
    auth payloads and falls back to 0, so omitting the field made every
    customer's balance render 0 no matter what their card held. Card resolution
    deliberately mirrors customer.py::get_loyalty (first program_type='loyalty'
    program, one card per partner) so the ticker and /api/v1/customer/loyalty
    can never disagree.
    """
    if not partner:
        return 0
    program = request.env['loyalty.program'].sudo().search(
        [('program_type', '=', 'loyalty')], limit=1
    )
    if not program:
        return 0
    card = request.env['loyalty.card'].sudo().search([
        ('partner_id', '=', partner.id),
        ('program_id', '=', program.id),
    ], limit=1)
    return card.points or 0


def _user_json(user):
    """Shared `user` payload for the login/register/google/verify responses.

    `date_of_birth` feeds the storefront checkout autofill: form.astro fills
    the 21+ birth-date field from this key and only makes the customer type
    it when the account carries none. It reads web_date_of_birth — the same
    write-once field registration stamps — serialized ISO (yyyy-mm-dd), empty
    string when unset (the FE treats any falsy value as "not on file").
    """
    partner = user.partner_id
    dob = partner.web_date_of_birth
    return {
        'id': user.id,
        'name': partner.name,
        'email': partner.email or user.login.removeprefix(WEB_LOGIN_PREFIX),
        'phone': partner.phone or '',
        'partner_id': partner.id,
        'is_internal': not user.share,
        'loyalty_points': _loyalty_points(partner),
        'date_of_birth': fields.Date.to_string(dob) if dob else '',
    }


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


# Advisory-lock namespace for web signups. pg_advisory_xact_lock takes
# (classid, objid) int4s; a fixed classid keeps this path's per-login locks
# from colliding with any other advisory lock in the database. Value is
# ASCII 'mint' as an int4.
_SIGNUP_LOCK_CLASS = 1836216180


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


def unexpected_error_response(user_message, log_context, status=500):
    """Log an unexpected exception and answer with text safe to show a customer.

    Call from inside an `except` block. Interpolating str(exc) into the
    response — which is what these handlers used to do — puts Python, psycopg
    and Odoo internals in front of shoppers: /rewards alerted the `error` field
    verbatim, so a failed redemption could read "Redemption failed: Odoo Server
    Error". The storefront should never name the backend.

    The exception text belongs in the log, where support can read it. The
    customer gets a stable sentence plus a short reference that appears on both
    sides, so a report of "reference 3f9a1c22" maps to one log line.

    This is for UNEXPECTED failures only. A UserError is a deliberate,
    human-readable message ("Email already registered") and should still be
    passed through with str(e) — that is information the customer needs.
    """
    ref = uuid.uuid4().hex[:8]
    # .exception() attaches the active traceback; the caller is in an except block.
    _logger.exception('%s [ref=%s]', log_context, ref)
    return error_response(
        '%s Please try again — if it keeps happening, quote reference %s.' % (user_message, ref),
        status,
    )


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
            'user': _user_json(user),
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

        # Dutchie identity link. The IdScanner reads the PDF417 on the back of
        # a US licence and posts documentNumber; 91.0% of the 1.79M synced
        # partners key on 'dl:<DL>' (5.1% mj:, 3.4% nd:, 0.4% ph:), so the DL
        # is the only match key worth carrying from a scan.
        #
        # We stamp the key and let the roster sync's dedup do the linking. We
        # do NOT bind this account to a matched partner here, for the same
        # reason the comment above refuses a client-supplied 'id_scanned':
        # documentNumber arrives in the request body and nothing server-side
        # has verified a document was scanned. Binding on it would let anyone
        # POST a 9-digit DL and inherit that person's purchase history and
        # loyalty balance. A matched partner that ALREADY has a web login is a
        # claimed account — flag it for human review rather than touch it.
        identity_key = self._dutchie_identity_key(data, name, dob)

        # Bind to the POS record dutchie_lookup matched, when the client
        # returns that endpoint's signed token. The token — not the DL in this
        # body — is what authorises the bind: it proves the server itself
        # matched an unclaimed record for this exact licence minutes ago, so a
        # bare POSTed DL still cannot claim anyone (see the posture note above
        # _create_web_user). The bind re-checks every property under this
        # transaction and falls back to a clean account if anything moved.
        link_partner_id = None
        raw_link_token = (data.get('link_token') or '').strip()
        if raw_link_token and identity_key:
            link_partner_id = self._verify_link_token(raw_link_token, identity_key)
            if not link_partner_id:
                # Forged, expired, or minted for a different licence. Not fatal
                # — just sign them up without the history.
                _logger.warning('register: rejected link_token for %s', identity_key)

        # A claimed duplicate still needs review, but only when we did NOT just
        # bind: the bind path already refuses claimed records and flags the
        # partner it linked, so flagging here too would mark it twice.
        if identity_key and not link_partner_id:
            self._flag_identity_collision(identity_key)

        try:
            user = self._create_web_user(
                email=email, name=name, phone=phone,
                web_dob=dob, age_method=method, age_source='web_register',
                identity_key=identity_key,
                link_partner_id=link_partner_id,
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

            # Welcome free pre-roll — issue the customer's unique, single-use
            # coupon at signup rather than waiting up to 30 min for the sweep
            # cron. _issue_welcome_preroll is idempotent and self-gates on the
            # mint.welcome_preroll config (no-op until ops enables it), and the
            # cron stays as a backstop. Best-effort + guarded: a Dutchie/config
            # hiccup must never 500 an already-committed signup.
            try:
                request.env['mint.discount'].sudo()._issue_welcome_preroll(
                    user.partner_id.sudo())
            except Exception as e:
                _logger.warning('Welcome pre-roll issue failed for partner %s: %s',
                                user.partner_id.id, e)

            return json_response({
                'token': token,
                'user': _user_json(user),
            }, status=201)

        except UserError as e:
            # Deliberate, human-readable validation ("Email already
            # registered") — the customer needs to read this one.
            return error_response(str(e))
        except Exception:
            return unexpected_error_response(
                'Could not complete your registration.',
                'Registration failed',
            )

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
            'user': _user_json(user),
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

    @staticmethod
    def _dutchie_identity_key(data, name, dob):
        """Build x_dutchie_identity_key for a web signup.

        MUST stay byte-identical to mint_dutchie_sync's
        DutchieSyncCheckpoint._identity_key, which produced the 1.79M keys we
        match against — a different normalisation silently matches nothing.
        That builder is DL > MJ state id > Name+DOB > phone; a web signup only
        ever has the first and third:

            dl:<DL uppercased>
            nd:<NAME uppercased>|<MM/DD/YYYY>

        Note the date format: the roster (and therefore every stored nd: key)
        uses MM/DD/YYYY, while the IdScanner posts ISO yyyy-mm-dd. Passing the
        ISO string straight through would build a key that matches no row.

        Returns None when neither a DL nor a name+DOB is available.
        """
        dl = (data.get('documentNumber') or data.get('document_number') or '').strip()
        if dl:
            return 'dl:' + dl.upper()
        nm = (name or '').strip().upper()
        if nm and dob:
            return 'nd:%s|%s' % (nm, dob.strftime('%m/%d/%Y'))
        return None

    @staticmethod
    def _flag_identity_collision(identity_key):
        """Mark an already-claimed partner sharing this identity for review.

        Best-effort: a signup must never fail because the review flag couldn't
        be written. Only partners that already carry a portal login are
        flagged — an unclaimed roster record needs no review, the sync's dedup
        will fold it in on the shared key.
        """
        try:
            Partner = request.env['res.partner'].sudo()
            matches = Partner.search([('x_dutchie_identity_key', '=', identity_key)], limit=10)
            if not matches:
                return
            claimed = request.env['res.users'].sudo().search([
                ('partner_id', 'in', matches.ids),
            ]).mapped('partner_id')
            if claimed and 'x_merge_needs_review' in Partner._fields:
                claimed.with_context(
                    tracking_disable=True, mail_notrack=True
                ).write({'x_merge_needs_review': True})
                _logger.info(
                    'register: identity %s already claimed by partner(s) %s — flagged for review',
                    identity_key, claimed.ids,
                )
        except Exception as e:  # noqa: BLE001 - never break signup on a flag
            _logger.warning('register: identity collision flag failed for %s: %s',
                            identity_key, e)

    # ------------------------------------------------------------------
    # Dutchie DL link (Odoo #105117)
    #
    # Binds a web signup to the POS record the roster sync already built, so a
    # returning customer sees their purchase history instead of an empty
    # account. Matching is on 'dl:<DL>' — 91% of the 1.79M synced partners key
    # on it, and DOB is not an option (2 of 1.63M dl: partners carry one).
    #
    # SECURITY POSTURE — read before widening this.
    #
    # A DL alone does NOT prove identity. The IdScanner runs client-side, so
    # documentNumber arrives in a request body and nothing here can tell a real
    # PDF417 read from a typed string. Florida and Michigan (both Mint markets)
    # derive DL numbers algorithmically from name + DOB + gender, so a computed
    # DL is within reach of anyone who knows those. The accepted control is
    # OUT OF BAND: staff confirm the physical ID at the register, which already
    # happens on every cannabis sale by law. Every link is therefore stamped
    # x_merge_needs_review so there is something to confirm against, and the
    # bind is deliberately kept cheap to reverse (no data is merged or moved —
    # only a portal login is attached to an existing partner).
    #
    # What that buys, and what it does not: it bounds the LOSS (an attacker
    # gains a provisional link a budtender will refuse) but not the
    # DISCLOSURE — the lookup response itself reveals visit/spend totals to
    # whoever holds the DL. That is why the summary below is coarse (counts and
    # dates only, never line items) and why claimed accounts are refused
    # outright. Loyalty balance is not exposed and is $0 on every record today;
    # if balances are ever populated, this endpoint must NOT start returning
    # them without a real second factor (a code to the contact on file).
    # ------------------------------------------------------------------

    # Short: the token only has to survive the walk from /join to /register.
    LINK_TOKEN_TTL = 900  # seconds

    @staticmethod
    def _link_secret():
        """Server-side signing key. `database.secret` never leaves Odoo."""
        return (request.env['ir.config_parameter'].sudo()
                .get_param('database.secret') or '')

    def _sign_link_token(self, partner_id, identity_key):
        """Sign a partner_id so the client cannot pick who it links to.

        Without this the browser would hand /register a bare partner_id and
        anyone could claim any record. The DL is folded into the signature so a
        token minted for one licence cannot be replayed against another.
        """
        exp = int(time.time()) + self.LINK_TOKEN_TTL
        msg = '%d|%d|%s' % (partner_id, exp, identity_key)
        sig = hmac.new(self._link_secret().encode(), msg.encode(),
                       hashlib.sha256).hexdigest()
        return '%d.%d.%s' % (partner_id, exp, sig)

    def _verify_link_token(self, token, identity_key):
        """Return the signed partner_id, or None if absent/forged/expired."""
        try:
            pid_s, exp_s, sig = (token or '').split('.', 2)
            partner_id, exp = int(pid_s), int(exp_s)
        except (ValueError, AttributeError):
            return None
        if exp < time.time():
            return None
        msg = '%d|%d|%s' % (partner_id, exp, identity_key)
        good = hmac.new(self._link_secret().encode(), msg.encode(),
                        hashlib.sha256).hexdigest()
        # compare_digest: don't leak the signature a byte at a time.
        if not hmac.compare_digest(good, sig):
            return None
        return partner_id

    @staticmethod
    def _surname_matches(scanned_last, partner_name):
        """Require the licence surname to appear in the roster name.

        This is an anti-enumeration check, not proof of identity: someone
        holding the licence already knows the name. What it stops is a blind
        sweep of candidate DL numbers — a random hit still has to carry the
        right surname, which turns "guess a DL" into "guess a DL AND its owner".

        Deliberately a token-contains rather than equality. Roster names are
        dirty (mixed case, middle names, business names, 'SMITH, JOHN'), so
        equality would reject real customers; requiring the surname to appear
        as a whole token keeps that noise from causing false negatives without
        letting an arbitrary name through.

        Hyphens and apostrophes are counted BOTH ways — 'O-BRIEN' offers
        {OBRIEN, O, BRIEN}. Splitting on them alone would lock out every
        O'Brien (licence 'O'BRIEN' normalises to OBRIEN, which is not a piece
        of O|BRIEN); keeping only the whole token would lock out the
        'SMITH-JONES' who scans as JONES. Each candidate is still a real name
        component, and the >=2 char floor keeps the fragments from matching
        noise.
        """
        norm = lambda s: re.sub(r'[^A-Z]', '', (s or '').upper())
        last = norm(scanned_last)
        # Sub-2 chars normalises to noise that matches almost anything.
        if len(last) < 2:
            return False
        tokens = set()
        for word in re.split(r'[\s,]+', partner_name or ''):
            tokens.add(norm(word))                    # O'BRIEN -> OBRIEN
            for piece in re.split(r"[-']+", word):    # SMITH-JONES -> SMITH, JONES
                tokens.add(norm(piece))
        return last in {t for t in tokens if t}

    @http.route('/api/v1/auth/dutchie-lookup', type='http', auth='none',
                methods=['POST', 'OPTIONS'], csrf=False, cors='*')
    def dutchie_lookup(self, **kw):
        """Find the unclaimed POS record for a scanned licence.

        Answers only "is there history to link, and roughly how much" — never
        who the person is (the caller scanned their licence, so a name would
        tell them nothing new) and never what they bought. On any refusal the
        response is an indistinguishable {'found': False}: telling a caller
        *why* (claimed / ambiguous / wrong surname) is itself a disclosure.
        """
        if request.httprequest.method == 'OPTIONS':
            return json_response({})

        gate = self._require_fe_key()
        if gate:
            return gate

        try:
            data = json.loads(request.httprequest.data)
        except (json.JSONDecodeError, TypeError):
            return error_response('Invalid JSON body')

        identity_key = self._dutchie_identity_key(data, None, None)
        last_name = (data.get('lastName') or data.get('last_name') or '').strip()
        if not identity_key or not identity_key.startswith('dl:') or not last_name:
            return json_response({'found': False})

        Partner = request.env['res.partner'].sudo()
        if 'x_dutchie_identity_key' not in Partner._fields:
            # mint_dutchie_sync absent (not in depends — see _create_web_user).
            return json_response({'found': False})

        # Only UNCLAIMED roster records are linkable, so scope the search to
        # them rather than filtering after — the distinction is load-bearing.
        #
        # register() stamps x_dutchie_identity_key on every new web partner (so
        # the roster sync can dedup later), which means each signup ADDS a row
        # carrying that licence's key. Matching on the key alone therefore saw
        # the roster record *plus* every account already created from it, tripped
        # the ambiguity guard, and refused — so a customer could link exactly
        # once and was locked out forever after. Observed in prod: one licence
        # accumulated 3 rows within a day of testing.
        #
        # Those extra rows all carry a login, so excluding claimed partners here
        # both restores matching and preserves the original refusal: an identity
        # someone has already signed up on is not linkable, it's a takeover.
        # What remains is genuine ambiguity — two roster rows, no logins, same
        # licence — which is still refused below because there is no safe way to
        # guess between two real people.
        #
        # limit=2: enough to detect ambiguity, no need to page 1.6M rows.
        matches = Partner.search(
            [('x_dutchie_identity_key', '=', identity_key),
             ('user_ids', '=', False)],
            limit=2,
        )
        if len(matches) != 1:
            if len(matches) > 1:
                _logger.warning('dutchie-lookup: ambiguous key %s -> %s',
                                identity_key, matches.ids)
            return json_response({'found': False})

        partner = matches

        if not self._surname_matches(last_name, partner.name):
            _logger.info('dutchie-lookup: surname mismatch for %s', identity_key)
            return json_response({'found': False})

        visits = partner.x_dutchie_visit_count or 0
        spend = partner.x_dutchie_total_spend or 0.0
        if visits <= 0 and not spend:
            # Nothing worth linking; don't prompt for a no-op.
            return json_response({'found': False})

        # Last visit isn't a partner field — derive it from the purchases.
        last_visit = False
        Purchase = request.env.get('mint.dutchie.purchase')
        if Purchase is not None:
            row = Purchase.sudo().search_read(
                [('partner_id', '=', partner.id)], ['date'],
                order='date desc', limit=1,
            )
            if row:
                last_visit = row[0]['date']

        _logger.info('dutchie-lookup: offered partner %s (%s) visits=%s',
                     partner.id, identity_key, visits)
        return json_response({
            'found': True,
            'link_token': self._sign_link_token(partner.id, identity_key),
            'summary': {
                'visits': visits,
                'total_spend': round(spend, 2),
                'first_visit': partner.x_dutchie_first_visit or False,
                'last_visit': last_visit,
            },
        })

    def _create_web_user(self, email, name, phone='', web_dob=None,
                         age_method=None, age_source=None, identity_key=None,
                         link_partner_id=None):
        """Create a fresh partner + portal user for a web-site signup.

        Always creates a NEW res.partner (doesn't reuse an existing one that
        happens to have the same email) so employees shopping the consumer
        site get a separate customer identity from their staff partner.

        ``identity_key`` stamps x_dutchie_identity_key (see
        mint_dutchie_sync._identity_key) so the roster sync's existing dedup
        can link this signup to the same person's POS records. It is only a
        match key — it does NOT reuse an existing partner here; see
        _dutchie_identity_key() for why a client-supplied DL is not trusted
        to bind an account on its own.

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

        # Serialize every concurrent signup for this login before touching
        # res_users. The ON CONFLICT guards below cannot do it on their own:
        # the website module redefines res_users_login_key as
        # UNIQUE (login, website_id) and these INSERTs leave website_id NULL,
        # and in Postgres NULLs are never equal — so the constraint never
        # matches, ON CONFLICT never fires, and BOTH racers insert. That is
        # how one double-submitted signup produced two users and two partners
        # (8 duplicate login pairs in prod as of 2026-07-23).
        #
        # A transaction-scoped advisory lock makes the second racer wait for
        # the first to commit, so it observes the winning row instead of
        # racing it. Released automatically on commit/rollback. The classid
        # namespaces the lock to this signup path so it cannot collide with
        # another feature's advisory lock.
        request.env.cr.execute(
            "SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
            (_SIGNUP_LOCK_CLASS, login),
        )

        # Holding that lock, an account for this login may already exist: the
        # first racer just committed, or a pre-guard duplicate is on file.
        # Reuse it for EVERY path below — the DL-link branch included, since
        # _attach_login's ON CONFLICT misses for the same website_id-is-NULL
        # reason and would otherwise insert a second row for one person.
        # ORDER BY id keeps the winner deterministic where historical
        # duplicates remain.
        request.env.cr.execute(
            "SELECT id FROM res_users WHERE login = %s ORDER BY id LIMIT 1",
            (login,),
        )
        # Residual (pre-existing, unchanged by this guard): register() writes
        # the submitted password onto whatever user it gets back, so a race
        # loser sets its password on the winner's account. Harmless for the
        # real case this fixes — one person double-submitting one form sends
        # the same password twice — and register()'s 409 guard already blocks
        # a second signup on an established login. Left as-is deliberately;
        # closing it means restructuring register(), not this guard.
        already = request.env.cr.fetchone()
        if already:
            request.env['res.users'].sudo().invalidate_model()
            return request.env['res.users'].sudo().browse(already[0])

        # --- Dutchie DL link: attach to the existing POS partner ------------
        # A verified link_token means dutchie_lookup already matched an
        # unclaimed roster record on 'dl:<DL>'. Reuse that partner instead of
        # creating a new one, so x_dutchie_purchase_ids (and the visit/spend
        # totals hanging off it) are simply THERE — no merge, no data copy.
        # Attaching a login is also the cheapest thing to undo if a budtender
        # rejects the link at the register.
        if link_partner_id:
            linked = self._bind_link_partner(
                link_partner_id, identity_key, email, name, phone, web_dob,
                age_method, age_source,
            )
            if linked:
                return self._attach_login(linked, login, main_company)
            # Re-check failed (record claimed/changed since lookup) — fall
            # through and create a clean account rather than fail the signup.
            _logger.warning('register: DL link to partner %s rejected at bind; '
                            'creating a fresh account', link_partner_id)

        # --- MR-1089: adopt an unclaimed Dutchie customer by email/phone ----
        # Without a scan there is no link_token, and the identity keys can
        # never collide by construction ('nd:NAME|DOB' here vs 'dl:<licence>'
        # on the roster), so every no-scan signup used to be orphaned from the
        # customer's own loyalty and purchase history.
        #
        # This match is WEAKER than the token path — email/phone are asserted
        # by the request body, not verified — so the adopted record is stamped
        # x_merge_needs_review and confirmed by staff at the register. It can
        # only ever touch a record that backs no login, so it cannot take over
        # an existing account.
        adopt = self._find_linkable_dutchie_partner(email, phone)
        if adopt:
            adopted = self._adopt_dutchie_partner(
                adopt, email, name, phone, web_dob, age_method, age_source,
            )
            if adopted:
                return self._attach_login(adopted, login, main_company)

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
        # x_dutchie_identity_key is declared by mint_dutchie_sync, which is NOT
        # in this module's depends (adding it would couple the consumer signup
        # API to the roster importer). Both are installed on prod, but an
        # unknown key in create() raises — so probe _fields rather than let a
        # missing module take down every signup.
        if identity_key and 'x_dutchie_identity_key' in request.env['res.partner']._fields:
            partner_vals['x_dutchie_identity_key'] = identity_key

        partner = request.env['res.partner'].sudo().with_context(
            mail_create_nosubscribe=True,
            mail_create_nolog=True,
            tracking_disable=True,
            mail_notrack=True,
        ).create(partner_vals)
        request.env.cr.flush()

        # Raw INSERT to bypass signup/mail hooks that conflict with the
        # transaction. No ON CONFLICT clause: res_users_login_key is
        # UNIQUE (login, website_id) with website_id NULL here, so it could
        # never detect the collision anyway. The advisory lock plus the
        # existence check above are what guarantee one row per login.
        request.env.cr.execute("""
            INSERT INTO res_users (login, password, company_id, partner_id,
                                   active, share, create_uid, write_uid,
                                   create_date, write_date, notification_type)
            VALUES (%s, '', %s, %s, true, true, 1, 1, NOW(), NOW(), 'email')
            RETURNING id
        """, (login, main_company.id, partner.id))
        user_id = request.env.cr.fetchone()[0]

        request.env.cr.execute("""
            INSERT INTO res_company_users_rel (cid, user_id)
            VALUES (%s, %s)
            ON CONFLICT DO NOTHING
        """, (main_company.id, user_id))

        request.env['res.users'].sudo().invalidate_model()
        return request.env['res.users'].sudo().browse(user_id)

    def _find_linkable_dutchie_partner(self, email, phone):
        """Find an existing Dutchie customer partner a web signup may adopt,
        so the account inherits that customer's loyalty and purchase history
        instead of being orphaned (MR-1089). Returns a partner or None.

        STRICT match: the signup must supply BOTH an email and a usable phone,
        and each must independently resolve to exactly ONE unclaimed Dutchie
        customer — the SAME one. Any weaker outcome returns None and the
        caller creates a clean account.

        Deliberately stricter than checkout.py::_find_partner (which takes
        phone OR email). Checkout matches per-order; this binds a LOGIN, so a
        wrong match hands over the customer's history permanently. Requiring
        two independent identifiers to agree means knowing just a victim's
        email — or being assigned a recycled phone number — is not enough.

        Guarded so this can never claim an account:

          * the partner must already be a Dutchie customer
            (``x_dutchie_customer_id`` set), and
          * it must not already back a login — an employee or an existing web
            customer keeps a disjoint identity, and
          * an ambiguous hit on either identifier (2+ candidates) is refused
            rather than guessed.

        SECURITY POSTURE: unlike the link_token path, the email/phone here
        come straight from the request body and nothing has verified the
        caller controls them. Adoption is therefore PROVISIONAL — the caller
        stamps x_merge_needs_review so staff confirm the physical ID at the
        register before the link is trusted.

        No-ops (returns None) when the Dutchie fields aren't installed, so the
        module still works without mint_dutchie_sync.
        """
        Partner = request.env['res.partner'].sudo()
        if 'x_dutchie_customer_id' not in Partner._fields:
            return None

        digits = ''.join(c for c in (phone or '') if c.isdigit())
        if not email or len(digits) < 10:
            # Both identifiers are required — one alone never adopts.
            return None

        # Unclaimed is part of the domain, not a post-filter, so an ambiguity
        # count is computed over adoptable records only.
        base = [('x_dutchie_customer_id', '!=', False), ('user_ids', '=', False)]
        # limit=2 is enough to tell "exactly one" from "ambiguous".
        by_phone = Partner.search(
            base + [('phone', 'ilike', digits[-10:])], order='id asc', limit=2)
        by_email = Partner.search(
            base + [('email', '=ilike', email)], order='id asc', limit=2)

        if len(by_phone) != 1 or len(by_email) != 1:
            # Zero matches, or ambiguous on an identifier — never guess.
            return None
        if by_phone.id != by_email.id:
            # The identifiers point at different people: strong evidence this
            # signup is not the roster customer. Refuse.
            _logger.info('adopt: email/phone disagree (%s vs %s) — no adoption',
                         by_email.id, by_phone.id)
            return None

        match = by_phone
        # Re-assert unclaimed (belt-and-braces; also re-checked in-transaction
        # by _adopt_dutchie_partner).
        if match.user_ids:
            return None
        return match

    def _adopt_dutchie_partner(self, partner, email, name, phone, web_dob,
                               age_method, age_source):
        """Prepare an email/phone-matched roster partner for a portal login.

        Mirrors _bind_link_partner, minus the identity-key assertion: that
        check is specific to the DL-token flow (the roster record is keyed
        'dl:<licence>' while a web signup computes 'nd:NAME|DOB', so the keys
        never match here by construction). The unclaimed re-check is kept —
        that is the property which stops an adoption from claiming an account.
        """
        partner = partner.exists()
        if not partner:
            return None
        # TOCTOU: someone may have claimed it since the match.
        if partner.user_ids:
            _logger.warning('adopt: partner %s claimed since match', partner.id)
            return None

        vals = {
            'is_web_customer': True,
            'customer_rank': 1,
            # Provisional until a human matches the licence to the record.
            'x_merge_needs_review': True,
        }
        # Never overwrite roster data with signup data — only fill gaps.
        if not partner.email and email:
            vals['email'] = email
        if not partner.phone and phone:
            vals['phone'] = phone
        if web_dob and not partner.web_date_of_birth:
            vals.update({
                'web_date_of_birth': web_dob,
                'age_verified': True,
                'age_verified_at': fields.Datetime.now(),
                'age_verification_method': age_method or 'self_attested',
                'age_verification_source': age_source or 'web_register',
            })
        if 'x_merge_needs_review' not in partner._fields:
            vals.pop('x_merge_needs_review')

        partner.with_context(
            tracking_disable=True, mail_notrack=True,
        ).write(vals)
        request.env.cr.flush()
        _logger.info('adopt: bound Dutchie partner %s to new login for %s '
                     '(flagged for review)', partner.id, email)
        return partner

    def _bind_link_partner(self, partner_id, identity_key, email, name, phone,
                           web_dob, age_method, age_source):
        """Re-check and prepare a roster partner for a portal login.

        The token proves dutchie_lookup approved THIS partner, but it was
        minted up to LINK_TOKEN_TTL ago against data read outside this
        transaction — so every safety property is re-asserted here rather than
        trusted from the token. Returns the partner, or None to fall back to a
        fresh account.
        """
        partner = request.env['res.partner'].sudo().browse(partner_id).exists()
        if not partner:
            return None
        # The signature covers the DL, but a partner's key could have been
        # rewritten by a roster re-sync between lookup and register.
        if partner.x_dutchie_identity_key != identity_key:
            _logger.warning('DL link: partner %s key changed since lookup', partner_id)
            return None
        # TOCTOU: someone may have claimed it in the meantime.
        if partner.user_ids:
            _logger.warning('DL link: partner %s claimed since lookup', partner_id)
            return None

        vals = {
            'is_web_customer': True,
            'customer_rank': 1,
            # The out-of-band control: staff confirm the physical ID at the
            # register. This is the flag they confirm against — a link is
            # provisional until a human has matched the licence to the record.
            'x_merge_needs_review': True,
        }
        # Never overwrite roster data with signup data: `name` is what the POS
        # has on the licence, and the email/phone on file may be how the store
        # reaches them. Only fill genuine gaps (64% have no contact at all).
        if not partner.email and email:
            vals['email'] = email
        if not partner.phone and phone:
            vals['phone'] = phone
        if web_dob and not partner.web_date_of_birth:
            vals.update({
                'web_date_of_birth': web_dob,
                'age_verified': True,
                'age_verified_at': fields.Datetime.now(),
                'age_verification_method': age_method or 'self_attested',
                'age_verification_source': age_source or 'web_register',
            })
        # x_merge_needs_review comes from mint_dutchie_sync, which is not in
        # this module's depends — same probe as the identity key write.
        if 'x_merge_needs_review' not in partner._fields:
            vals.pop('x_merge_needs_review')

        partner.with_context(
            tracking_disable=True, mail_notrack=True,
        ).write(vals)
        request.env.cr.flush()
        _logger.info('DL link: bound partner %s (%s) to new login for %s',
                     partner.id, identity_key, email)
        return partner

    def _attach_login(self, partner, login, main_company):
        """Attach a portal login to an EXISTING partner (the DL-link path).

        Mirrors the raw-INSERT above for the same reason (signup/mail hooks
        fight the transaction), with one deliberate difference: the race-loser
        branch must NEVER unlink. There the partner is disposable because we
        just made it; here it is the customer's roster record with their whole
        purchase history hanging off it, and unlinking would destroy real data
        to resolve a login collision.
        """
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
            # Race: this email already has an account. Hand back the winner and
            # leave the roster partner untouched (unlinked from this signup).
            request.env.cr.execute(
                "SELECT id FROM res_users WHERE login = %s", (login,)
            )
            user_id = request.env.cr.fetchone()[0]
            _logger.warning('DL link: login %s won by a concurrent signup; '
                            'partner %s left unlinked', login, partner.id)

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
            'user': _user_json(user),
        })
