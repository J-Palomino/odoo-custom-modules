# -*- coding: utf-8 -*-
"""Short-lived authorization codes and long-lived refresh tokens.

Both are stored as SHA-256 hashes. The raw value exists only in the HTTP
response that issues it, so read access to this table does not let an attacker
replay a grant.
"""
import hashlib
import logging
import secrets
from datetime import timedelta

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

# Auth codes travel through the browser address bar and are exchanged
# immediately, so their window is tight (RFC 6749 recommends <= 10 minutes;
# 60s is ample for a server-side exchange and much less to steal).
AUTH_CODE_TTL_SECONDS = 60


def hash_token(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


class MintOauthCode(models.Model):
    _name = "mint.oauth.code"
    _description = "OIDC Authorization Code"
    _order = "create_date desc"
    _rec_name = "code_hash"

    code_hash = fields.Char(required=True, index=True, readonly=True)
    client_id = fields.Many2one(
        "mint.oauth.client", required=True, ondelete="cascade", index=True
    )
    user_id = fields.Many2one("res.users", required=True, ondelete="cascade", index=True)
    redirect_uri = fields.Char(required=True)
    scope = fields.Char()
    nonce = fields.Char(help="Replayed into the ID token to bind it to the request.")
    code_challenge = fields.Char()
    code_challenge_method = fields.Char()
    expires_at = fields.Datetime(required=True, index=True)
    # Single use. We mark rather than delete so a replay attempt is detectable
    # and can invalidate the whole grant family (RFC 6819 §5.2.1.1).
    used_at = fields.Datetime(readonly=True)

    @api.model
    def _issue(self, client, user, redirect_uri, scope, nonce,
               code_challenge, code_challenge_method):
        raw = secrets.token_urlsafe(32)
        self.sudo().create({
            "code_hash": hash_token(raw),
            "client_id": client.id,
            "user_id": user.id,
            "redirect_uri": redirect_uri,
            "scope": scope,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": code_challenge_method,
            "expires_at": fields.Datetime.now() + timedelta(seconds=AUTH_CODE_TTL_SECONDS),
        })
        return raw

    @api.model
    def _consume(self, raw_code, client):
        """Look up and atomically burn a code. Returns the record or empty.

        Returns empty for unknown, expired, wrong-client, and already-used
        codes alike — the caller must not distinguish these to the client.
        """
        record = self.sudo().search([
            ("code_hash", "=", hash_token(raw_code or "")),
            ("client_id", "=", client.id),
        ], limit=1)
        if not record:
            return self.browse()
        if record.used_at:
            # Replay: the code was already exchanged. Kill every token minted
            # from this grant, because we cannot tell attacker from victim.
            _logger.warning(
                "OIDC authorization code replayed for client=%s user=%s — "
                "revoking descendant tokens",
                client.client_id, record.user_id.login,
            )
            self.env["mint.oauth.token"].sudo().search([
                ("code_id", "=", record.id)
            ]).write({"revoked": True})
            return self.browse()
        if record.expires_at < fields.Datetime.now():
            return self.browse()
        record.write({"used_at": fields.Datetime.now()})
        return record

    @api.autovacuum
    def _gc_codes(self):
        """Drop codes well past expiry; they carry a user linkage we needn't keep."""
        cutoff = fields.Datetime.now() - timedelta(days=1)
        stale = self.sudo().search([("expires_at", "<", cutoff)])
        _logger.info("Garbage-collecting %s expired OIDC auth codes", len(stale))
        stale.unlink()


class MintOauthToken(models.Model):
    _name = "mint.oauth.token"
    _description = "OIDC Refresh Token"
    _order = "create_date desc"
    _rec_name = "token_hash"

    token_hash = fields.Char(required=True, index=True, readonly=True)
    client_id = fields.Many2one(
        "mint.oauth.client", required=True, ondelete="cascade", index=True
    )
    user_id = fields.Many2one("res.users", required=True, ondelete="cascade", index=True)
    code_id = fields.Many2one(
        "mint.oauth.code", ondelete="set null",
        help="Grant this token descends from, so a code replay can revoke it.",
    )
    scope = fields.Char()
    expires_at = fields.Datetime(required=True, index=True)
    revoked = fields.Boolean(default=False, index=True)

    @api.model
    def _issue(self, client, user, scope, code=None):
        raw = secrets.token_urlsafe(48)
        self.sudo().create({
            "token_hash": hash_token(raw),
            "client_id": client.id,
            "user_id": user.id,
            "code_id": code.id if code else False,
            "scope": scope,
            "expires_at": fields.Datetime.now() + timedelta(
                seconds=client.refresh_token_ttl or 1209600
            ),
        })
        return raw

    @api.model
    def _resolve(self, raw_token, client):
        """Return a live, unrevoked token record, or empty."""
        record = self.sudo().search([
            ("token_hash", "=", hash_token(raw_token or "")),
            ("client_id", "=", client.id),
            ("revoked", "=", False),
        ], limit=1)
        if not record or record.expires_at < fields.Datetime.now():
            return self.browse()
        return record

    def action_revoke(self):
        return self.write({"revoked": True})

    @api.autovacuum
    def _gc_tokens(self):
        cutoff = fields.Datetime.now() - timedelta(days=30)
        stale = self.sudo().search([("expires_at", "<", cutoff)])
        _logger.info("Garbage-collecting %s expired OIDC refresh tokens", len(stale))
        stale.unlink()
