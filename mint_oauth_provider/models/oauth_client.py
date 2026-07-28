# -*- coding: utf-8 -*-
"""Registered OIDC relying parties.

Secrets are stored only as hashes — a database leak must not yield a working
client credential. The plaintext secret is shown exactly once, at generation
time, and is never persisted.
"""
import hashlib
import hmac
import logging
import secrets

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)


class MintOauthClient(models.Model):
    _name = "mint.oauth.client"
    _description = "OIDC Client (Relying Party)"
    _order = "name"

    name = fields.Char(required=True)
    client_id = fields.Char(
        required=True,
        index=True,
        copy=False,
        default=lambda self: secrets.token_urlsafe(16),
    )
    client_secret_hash = fields.Char(
        readonly=True, copy=False, groups="base.group_system"
    )
    # There is deliberately NO field holding the plaintext secret. It exists
    # only as the return value of _new_secret(), surfaced once in a
    # notification, and is never written to the database in any form.
    redirect_uri_ids = fields.One2many(
        "mint.oauth.redirect.uri", "client_id", string="Redirect URIs"
    )
    scopes = fields.Char(
        default="openid profile email",
        help="Space-separated scopes this client may request.",
    )
    active = fields.Boolean(default=True)

    # Token lifetimes, in seconds. Auth codes are deliberately short: they
    # travel through the browser's address bar.
    access_token_ttl = fields.Integer(default=3600, string="Access Token TTL (s)")
    refresh_token_ttl = fields.Integer(default=1209600, string="Refresh Token TTL (s)")

    _sql_constraints = [
        ("client_id_uniq", "unique(client_id)", "Client ID must be unique."),
    ]

    @api.constrains("scopes")
    def _check_scopes(self):
        for record in self:
            if "openid" not in (record.scopes or "").split():
                raise ValidationError(
                    _("An OIDC client must include the 'openid' scope.")
                )

    # ------------------------------------------------------------------
    def _new_secret(self):
        """Mint a secret, persist only its hash, and return the plaintext.

        The caller is the sole holder of the plaintext — nothing downstream
        stores it. Regenerating invalidates the previous secret immediately.
        """
        self.ensure_one()
        raw = secrets.token_urlsafe(32)
        self.sudo().write({"client_secret_hash": self._hash_secret(raw)})
        _logger.info("Rotated OIDC client secret for client_id=%s", self.client_id)
        return raw

    def action_generate_secret(self):
        """Issue a fresh secret and show it once."""
        self.ensure_one()
        raw = self._new_secret()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Client secret generated"),
                "message": _(
                    "Copy it now — it is stored only as a hash and cannot be "
                    "shown again: %s"
                ) % raw,
                "sticky": True,
                "type": "warning",
            },
        }

    @api.model
    def _hash_secret(self, raw):
        # Client secrets are high-entropy machine credentials (256 bits from
        # token_urlsafe(32)), not user passwords, so a plain SHA-256 is the
        # right tool: brute force is infeasible regardless of KDF, and we want
        # constant, cheap verification on every token request.
        return hashlib.sha256(raw.encode()).hexdigest()

    def _verify_secret(self, raw):
        """Constant-time secret check."""
        self.ensure_one()
        stored = self.sudo().client_secret_hash
        if not stored or not raw:
            return False
        return hmac.compare_digest(stored, self._hash_secret(raw))

    def _redirect_uri_allowed(self, uri):
        """Exact match only — no prefix or wildcard matching.

        Loose redirect matching is the classic OAuth account-takeover vector,
        so this is deliberately strict even though it means every environment
        must be registered explicitly.
        """
        self.ensure_one()
        return bool(uri) and uri in self.redirect_uri_ids.mapped("uri")

    @api.model
    def _find_active(self, client_id):
        if not client_id:
            return self.browse()
        return self.sudo().search(
            [("client_id", "=", client_id), ("active", "=", True)], limit=1
        )


class MintOauthRedirectUri(models.Model):
    _name = "mint.oauth.redirect.uri"
    _description = "OIDC Client Redirect URI"

    client_id = fields.Many2one(
        "mint.oauth.client", required=True, ondelete="cascade", index=True
    )
    uri = fields.Char(required=True)

    @api.constrains("uri")
    def _check_uri(self):
        for record in self:
            uri = record.uri or ""
            if not uri.startswith(("https://", "http://localhost", "http://127.0.0.1")):
                raise ValidationError(
                    _("Redirect URIs must use https, except on localhost: %s") % uri
                )
            if "#" in uri:
                raise ValidationError(
                    _("Redirect URIs must not contain a fragment: %s") % uri
                )
