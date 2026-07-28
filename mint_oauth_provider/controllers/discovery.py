# -*- coding: utf-8 -*-
"""OIDC discovery and JWKS endpoints.

These are the two unauthenticated, cacheable documents a relying party fetches
before it can talk to us. Penpot reads the discovery document from whatever we
set as PENPOT_OIDC_BASE_URI and derives every other endpoint from it.
"""
import json
import logging

from odoo import http
from odoo.http import request, Response

_logger = logging.getLogger(__name__)

# Issuer must match the `iss` claim we sign into ID tokens exactly, or verifiers
# reject them. Both derive from web.base.url — see _issuer().
WELL_KNOWN_PATH = "/.well-known/openid-configuration"


def json_response(payload, status=200, max_age=300):
    return Response(
        json.dumps(payload),
        status=status,
        content_type="application/json",
        headers=[("Cache-Control", "public, max-age=%d" % max_age)],
    )


class MintOidcDiscovery(http.Controller):

    @staticmethod
    def _issuer():
        """The issuer identifier, without a trailing slash.

        Anchored on web.base.url rather than the request Host so a request
        arriving via an unexpected hostname cannot mint tokens claiming to be
        from that host.
        """
        base = request.env["ir.config_parameter"].sudo().get_param("web.base.url") or ""
        return base.rstrip("/")

    @http.route(
        WELL_KNOWN_PATH,
        type="http", auth="public", methods=["GET"], csrf=False, save_session=False,
    )
    def openid_configuration(self, **kwargs):
        issuer = self._issuer()
        return json_response({
            "issuer": issuer,
            "authorization_endpoint": "%s/oauth2/authorize" % issuer,
            "token_endpoint": "%s/oauth2/token" % issuer,
            "userinfo_endpoint": "%s/oauth2/userinfo" % issuer,
            "jwks_uri": "%s/oauth2/jwks" % issuer,
            # No end_session_endpoint: RP-initiated logout is not implemented,
            # and advertising an endpoint that 404s makes conforming clients
            # fail at sign-out. Add the key only alongside the route.
            "scopes_supported": ["openid", "profile", "email"],
            "response_types_supported": ["code"],
            "response_modes_supported": ["query"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "subject_types_supported": ["public"],
            "id_token_signing_alg_values_supported": ["RS256"],
            "token_endpoint_auth_methods_supported": [
                "client_secret_post", "client_secret_basic",
            ],
            # S256 only. `plain` is permitted by RFC 7636 but offers no
            # protection against code interception, so we refuse it.
            "code_challenge_methods_supported": ["S256"],
            "claims_supported": [
                "sub", "iss", "aud", "exp", "iat", "nonce",
                "email", "email_verified", "name", "preferred_username",
            ],
        })

    @http.route(
        "/oauth2/jwks",
        type="http", auth="public", methods=["GET"], csrf=False, save_session=False,
    )
    def jwks(self, **kwargs):
        """Public halves of every active key.

        All active keys are published, not just the signing one, so tokens
        signed by a key we have since rotated away from still verify until
        they expire.
        """
        keys = request.env["mint.oauth.keypair"].sudo().search([("active", "=", True)])
        if not keys:
            # First request on a fresh install: mint the initial key so the
            # document is never empty (an empty JWKS breaks RP bootstrapping).
            keys = request.env["mint.oauth.keypair"].sudo()._get_signing_key()
        return json_response(keys._jwks())
