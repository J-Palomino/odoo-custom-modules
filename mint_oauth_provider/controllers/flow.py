# -*- coding: utf-8 -*-
"""Authorization Code flow with mandatory PKCE.

Threat model notes, because the details here are what stand between us and
account takeover:

* The redirect URI is validated BEFORE any redirect happens, and a bad one is
  rendered as an error page rather than redirected to. Redirecting an error to
  an unvalidated URI is the open-redirect that turns a bug into a takeover.
* PKCE S256 is mandatory even though Penpot is a confidential client. It costs
  nothing and removes the value of a stolen code entirely.
* Client authentication is required on /token. Both RFC 6749 methods are
  accepted (client_secret_basic and client_secret_post).
* Every secret comparison is constant-time.
* Group membership is re-checked at /authorize AND at /token refresh, so
  removing someone from the access group cuts them off at the next hop rather
  than at token expiry.
"""
import base64
import hashlib
import hmac
import json
import logging
import time
from urllib.parse import urlencode

from odoo import http
from odoo.http import request, Response

_logger = logging.getLogger(__name__)

ACCESS_GROUP = "mint_oauth_provider.group_oidc_user"


def _json(payload, status=200):
    return Response(
        json.dumps(payload),
        status=status,
        content_type="application/json",
        headers=[("Cache-Control", "no-store"), ("Pragma", "no-cache")],
    )


def _oauth_error(error, description, status=400):
    return _json({"error": error, "error_description": description}, status=status)


def _error_page(title, detail, status=400):
    """Terminal error shown in the browser.

    Used only when we cannot safely redirect back to the client — i.e. when the
    client_id or redirect_uri is itself untrustworthy.
    """
    html = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>Sign-in error</title></head><body "
        "style=\"font-family:system-ui,sans-serif;max-width:34rem;margin:4rem auto;"
        "padding:0 1rem;color:#171717\">"
        "<h1 style=\"font-size:1.25rem\">%s</h1>"
        "<p style=\"color:#525252\">%s</p>"
        "<p style=\"color:#a3a3a3;font-size:.875rem\">If you reached this from an "
        "application, its sign-in configuration is likely wrong. Nothing has been "
        "granted.</p></body></html>"
    ) % (title, detail)
    return Response(html, status=status, content_type="text/html; charset=utf-8")


class MintOidcFlow(http.Controller):

    # ------------------------------------------------------------------
    # /oauth2/authorize
    # ------------------------------------------------------------------
    @http.route("/oauth2/authorize", type="http", auth="user", methods=["GET"], csrf=False)
    def authorize(self, **params):
        """auth='user' is doing real work here.

        It means Odoo bounces an unauthenticated visitor to its own login,
        which is whatever Odoo is configured to use (currently Google). So the
        identity ultimately comes from the existing SSO rather than a second
        password store, and we never see a credential.
        """
        client_id = params.get("client_id")
        redirect_uri = params.get("redirect_uri")

        client = request.env["mint.oauth.client"]._find_active(client_id)
        if not client:
            _logger.warning("OIDC authorize: unknown client_id=%r", client_id)
            return _error_page(
                "Unknown application",
                "This application is not registered for sign-in.",
            )

        # Validate the redirect target BEFORE we are willing to redirect to it.
        if not client._redirect_uri_allowed(redirect_uri):
            _logger.warning(
                "OIDC authorize: redirect_uri %r not registered for client=%s",
                redirect_uri, client.client_id,
            )
            return _error_page(
                "Invalid redirect",
                "The application asked to return to an address that is not "
                "registered for it.",
            )

        state = params.get("state")

        def deny(error, description):
            """From here on, errors go back to the (validated) client."""
            query = {"error": error, "error_description": description}
            if state:
                query["state"] = state
            return request.redirect(
                "%s?%s" % (redirect_uri, urlencode(query)), code=303, local=False
            )

        if params.get("response_type") != "code":
            return deny("unsupported_response_type", "Only 'code' is supported.")

        challenge = params.get("code_challenge")
        method = params.get("code_challenge_method")
        if not challenge:
            return deny("invalid_request", "PKCE code_challenge is required.")
        if method != "S256":
            return deny(
                "invalid_request",
                "code_challenge_method must be S256; 'plain' is not accepted.",
            )

        requested = (params.get("scope") or "openid").split()
        allowed = set((client.scopes or "").split())
        if "openid" not in requested:
            return deny("invalid_scope", "The 'openid' scope is required.")
        if not set(requested).issubset(allowed):
            return deny(
                "invalid_scope",
                "Requested scope exceeds what this application may request.",
            )

        user = request.env.user
        if not user.has_group(ACCESS_GROUP):
            _logger.info(
                "OIDC authorize: user %s denied — not in %s", user.login, ACCESS_GROUP
            )
            return deny(
                "access_denied",
                "Your account does not have access to this application.",
            )

        code = request.env["mint.oauth.code"]._issue(
            client=client,
            user=user,
            redirect_uri=redirect_uri,
            scope=" ".join(requested),
            nonce=params.get("nonce"),
            code_challenge=challenge,
            code_challenge_method=method,
        )
        _logger.info(
            "OIDC authorize: issued code for user=%s client=%s", user.login, client.client_id
        )

        query = {"code": code}
        if state:
            query["state"] = state
        return request.redirect(
            "%s?%s" % (redirect_uri, urlencode(query)), code=303, local=False
        )

    # ------------------------------------------------------------------
    # /oauth2/token
    # ------------------------------------------------------------------
    @http.route("/oauth2/token", type="http", auth="public", methods=["POST"], csrf=False,
                save_session=False)
    def token(self, **params):
        client, err = self._authenticate_client(params)
        if err:
            return err

        grant_type = params.get("grant_type")
        if grant_type == "authorization_code":
            return self._grant_authorization_code(client, params)
        if grant_type == "refresh_token":
            return self._grant_refresh_token(client, params)
        return _oauth_error(
            "unsupported_grant_type",
            "Supported: authorization_code, refresh_token.",
        )

    # -- client authentication ----------------------------------------
    def _authenticate_client(self, params):
        """Both RFC 6749 §2.3.1 methods. Returns (client, error_response)."""
        client_id = params.get("client_id")
        client_secret = params.get("client_secret")

        auth_header = request.httprequest.headers.get("Authorization", "")
        if auth_header.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
                basic_id, _, basic_secret = decoded.partition(":")
            except Exception:
                return None, _oauth_error(
                    "invalid_client", "Malformed Basic authorization header.", 401
                )
            # Body params must not contradict the header.
            if client_id and client_id != basic_id:
                return None, _oauth_error(
                    "invalid_client", "Conflicting client credentials.", 401
                )
            client_id, client_secret = basic_id, basic_secret

        client = request.env["mint.oauth.client"]._find_active(client_id)
        if not client or not client._verify_secret(client_secret):
            # Same response either way: never reveal whether the id exists.
            _logger.warning("OIDC token: client authentication failed for %r", client_id)
            return None, _oauth_error(
                "invalid_client", "Client authentication failed.", 401
            )
        return client, None

    # -- authorization_code -------------------------------------------
    def _grant_authorization_code(self, client, params):
        code_record = request.env["mint.oauth.code"]._consume(
            params.get("code"), client
        )
        if not code_record:
            # Covers unknown, expired, wrong-client and replayed codes alike.
            return _oauth_error("invalid_grant", "Authorization code is not valid.")

        # The redirect_uri must match the one the code was issued against
        # (RFC 6749 §4.1.3) — otherwise a code stolen by one registered client
        # could be redeemed against another.
        if params.get("redirect_uri") != code_record.redirect_uri:
            return _oauth_error("invalid_grant", "redirect_uri mismatch.")

        verifier = params.get("code_verifier") or ""
        if not self._pkce_ok(verifier, code_record.code_challenge):
            _logger.warning(
                "OIDC token: PKCE verification failed for client=%s", client.client_id
            )
            return _oauth_error("invalid_grant", "PKCE verification failed.")

        user = code_record.user_id
        if not user.has_group(ACCESS_GROUP):
            return _oauth_error("access_denied", "User no longer has access.")

        return self._issue_tokens(client, user, code_record.scope,
                                  nonce=code_record.nonce, code=code_record)

    @staticmethod
    def _pkce_ok(verifier, challenge):
        """S256: BASE64URL(SHA256(verifier)) == challenge, compared constant-time."""
        if not verifier or not challenge:
            return False
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        return hmac.compare_digest(computed, challenge)

    # -- refresh_token -------------------------------------------------
    def _grant_refresh_token(self, client, params):
        record = request.env["mint.oauth.token"]._resolve(
            params.get("refresh_token"), client, token_type="refresh"
        )
        if not record:
            return _oauth_error("invalid_grant", "Refresh token is not valid.")

        user = record.user_id
        # Re-checked on every refresh: revoking group membership must actually
        # end access, not merely stop new logins.
        if not user.has_group(ACCESS_GROUP):
            record.action_revoke()
            return _oauth_error("access_denied", "User no longer has access.")

        # Rotate: the presented refresh token is burned as it is exchanged.
        record.action_revoke()
        return self._issue_tokens(client, user, record.scope, nonce=None)

    # -- token minting -------------------------------------------------
    def _issue_tokens(self, client, user, scope, nonce=None, code=None):
        import jwt

        Token = request.env["mint.oauth.token"]
        access = Token._issue(client, user, scope, token_type="access", code=code)
        refresh = Token._issue(client, user, scope, token_type="refresh", code=code)

        key = request.env["mint.oauth.keypair"]._get_signing_key()
        issuer = (
            request.env["ir.config_parameter"].sudo().get_param("web.base.url") or ""
        ).rstrip("/")
        now = int(time.time())
        claims = {
            "iss": issuer,
            "sub": str(user.id),
            "aud": client.client_id,
            "exp": now + (client.access_token_ttl or 3600),
            "iat": now,
            "auth_time": now,
        }
        if nonce:
            claims["nonce"] = nonce
        claims.update(self._profile_claims(user, scope))

        id_token = jwt.encode(
            claims, key.private_pem, algorithm="RS256", headers={"kid": key.kid}
        )
        _logger.info(
            "OIDC token: issued for user=%s client=%s scope=%r",
            user.login, client.client_id, scope,
        )
        return _json({
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": client.access_token_ttl or 3600,
            "refresh_token": refresh,
            "id_token": id_token,
            "scope": scope,
        })

    @staticmethod
    def _profile_claims(user, scope):
        """Claims for the granted scopes, and nothing beyond them."""
        granted = (scope or "").split()
        claims = {}
        if "email" in granted:
            claims["email"] = user.email or user.login
            # Odoo has no separate verification flag; these are provisioned
            # internal accounts, and the upstream IdP verified the address.
            claims["email_verified"] = True
        if "profile" in granted:
            claims["name"] = user.name
            claims["preferred_username"] = user.login
        return claims

    # ------------------------------------------------------------------
    # /oauth2/userinfo
    # ------------------------------------------------------------------
    @http.route("/oauth2/userinfo", type="http", auth="public",
                methods=["GET", "POST"], csrf=False, save_session=False)
    def userinfo(self, **params):
        header = request.httprequest.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return Response(
                json.dumps({"error": "invalid_token"}),
                status=401,
                content_type="application/json",
                headers=[("WWW-Authenticate", 'Bearer realm="oidc"')],
            )
        raw = header[7:].strip()

        record = request.env["mint.oauth.token"]._resolve(raw, token_type="access")
        if not record:
            return Response(
                json.dumps({"error": "invalid_token"}),
                status=401,
                content_type="application/json",
                headers=[("WWW-Authenticate", 'Bearer error="invalid_token"')],
            )

        user = record.user_id
        if not user.has_group(ACCESS_GROUP):
            record.action_revoke()
            return _json({"error": "invalid_token"}, status=401)

        payload = {"sub": str(user.id)}
        payload.update(self._profile_claims(user, record.scope))
        return _json(payload)
