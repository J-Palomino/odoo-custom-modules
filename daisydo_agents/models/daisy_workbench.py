"""Daisy Workbench sign-in: LGM (Odoo) accounts get their own daisy.plus key.

The workbench is a local app (127.0.0.1:7690) that runs Claude Code against
daisy.plus's Anthropic passthrough. Instead of every install pasting a shared
workspace key, the user signs in to Odoo and Odoo mints a key for that one
install:

1. The workbench opens ``/daisy/workbench/connect`` (auth=user). The user
   confirms, and Odoo redirects to ``http://127.0.0.1:<port>/auth/callback``
   with a one-time code (a pending record here).
2. The workbench POSTs the code to ``/daisy/workbench/token``. Only then is the
   daisy.plus key created, so an abandoned sign-in leaves no key behind.
3. Revoking deletes the key on daisy.plus. Deactivating a user revokes all of
   theirs.

daisy.plus keys are workspace-level, so per-user attribution lives in the key
name and in these records, not in daisy.plus itself. Neither the code nor the
key is stored in clear: both are kept as SHA-256 hashes.
"""
import hashlib
import logging
import re
import secrets
from datetime import timedelta

import requests

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError

_logger = logging.getLogger(__name__)

CODE_TTL = timedelta(minutes=2)
DEFAULT_MAX_ACTIVE = 5
DEFAULT_ANTHROPIC_URL = "https://daisy.plus/api/v1/anthropic"
_HOST_RE = re.compile(r"[^A-Za-z0-9._-]")


def _sha256(raw):
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class DaisyWorkbenchToken(models.Model):
    _name = "daisy.workbench.token"
    _description = "Daisy Workbench Install"
    _order = "create_date desc"

    user_id = fields.Many2one(
        "res.users", required=True, index=True, ondelete="cascade", readonly=True,
    )
    hostname = fields.Char(readonly=True)
    state = fields.Selection(
        [("pending", "Pending"), ("issued", "Active"), ("revoked", "Revoked")],
        default="pending", required=True, index=True, readonly=True,
    )
    code_hash = fields.Char(index=True, readonly=True, groups="base.group_system")
    code_expires = fields.Datetime(readonly=True)
    key_hash = fields.Char(index=True, readonly=True, groups="base.group_system")
    daisy_key_id = fields.Char(string="daisy.plus Key ID", readonly=True)
    key_name = fields.Char(readonly=True)
    issued_at = fields.Datetime(readonly=True)
    revoked_at = fields.Datetime(readonly=True)

    # ---- config ----

    @api.model
    def _param(self, key, default):
        return self.env["ir.config_parameter"].sudo().get_param(key, default)

    @api.model
    def _anthropic_url(self):
        return self._param("daisy.workbench.anthropic_url", DEFAULT_ANTHROPIC_URL).rstrip("/")

    @api.model
    def _max_active(self):
        try:
            return int(self._param("daisy.workbench.max_active", DEFAULT_MAX_ACTIVE))
        except ValueError:
            return DEFAULT_MAX_ACTIVE

    # ---- eligibility ----

    @api.model
    def _check_eligible(self, user):
        """Same gate as agent auto-provisioning: an active internal user who is an employee."""
        if not user or user.share or not user.active:
            raise AccessError("Daisy Workbench is for internal LGM accounts only.")
        if not self.env["hr.employee"].sudo().search_count([("user_id", "=", user.id)]):
            raise AccessError(
                "Your account is not linked to an employee record. Ask IT to link it, then try again."
            )
        active = self.sudo().search_count([("user_id", "=", user.id), ("state", "=", "issued")])
        if active >= self._max_active():
            raise UserError(
                "You already have %d active Workbench installs. Sign out of one "
                "(or ask IT to revoke it), then try again." % active
            )

    # ---- flow ----

    @api.model
    def _start(self, user, hostname):
        """Create a pending record and return the raw one-time code."""
        self._check_eligible(user)
        code = secrets.token_urlsafe(32)
        self.sudo().create({
            "user_id": user.id,
            "hostname": _HOST_RE.sub("", hostname or "")[:64] or "unknown",
            "code_hash": _sha256(code),
            "code_expires": fields.Datetime.now() + CODE_TTL,
        })
        return code

    @api.model
    def _redeem(self, code):
        """Exchange a one-time code for a freshly minted daisy.plus key.

        Returns the payload for the workbench, or None for any invalid code
        (unknown, expired, replayed) so callers cannot tell them apart.
        """
        if not code or not isinstance(code, str):
            return None
        rec = self.sudo().search(
            [("code_hash", "=", _sha256(code)), ("state", "=", "pending")], limit=1
        )
        if not rec:
            return None
        # Burn the code before any network call, so a retry cannot redeem it twice.
        rec.write({"code_hash": False})
        if rec.code_expires < fields.Datetime.now():
            rec.unlink()
            return None
        user = rec.user_id
        rec._check_eligible(user)

        workspace_key = self.env["daisy.agent"].sudo()._workspace_key()
        if not workspace_key:
            raise UserError("daisy.plus workspace key is not configured (daisy.global_api_key).")
        prefix = self._param("daisy.workbench.key_prefix", "workbench")
        key_name = "%s:%s@%s#%d" % (prefix, user.login, rec.hostname, rec.id)
        info = self.env["daisy.ai.service"].sudo().create_apikey(workspace_key, key_name)
        api_key, key_id = info.get("apiKey"), info.get("id")
        if not api_key or info.get("keyName") != key_name:
            # create_apikey falls back to the last key in the list when nothing
            # matches; never hand out a key we cannot prove we just created.
            rec.unlink()
            raise UserError("daisy.plus did not return the new key.")
        rec.write({
            "state": "issued",
            "daisy_key_id": key_id,
            "key_name": key_name,
            "key_hash": _sha256(api_key),
            "issued_at": fields.Datetime.now(),
            "code_expires": False,
        })
        _logger.info("Daisy Workbench key issued: %s (record %s)", key_name, rec.id)
        return {
            "api_key": api_key,
            "base_url": self._anthropic_url(),
            "user": {"login": user.login, "name": user.name},
            "install_id": rec.id,
        }

    def _revoke(self):
        """Delete each key on daisy.plus, then mark the record revoked.

        A key already gone on daisy.plus (404) still counts as revoked. Any
        other failure leaves the record issued so it can be retried.
        """
        workspace_key = self.env["daisy.agent"].sudo()._workspace_key()
        base = self.env["daisy.ai.service"].sudo()._get_daisy_api_base()
        for rec in self.sudo().filtered(lambda r: r.state != "revoked"):
            if rec.daisy_key_id:
                resp = requests.delete(
                    "%s/apikey/%s" % (base, rec.daisy_key_id),
                    headers={"Authorization": "Bearer %s" % workspace_key},
                    timeout=30,
                )
                if resp.status_code not in (200, 204, 404):
                    raise UserError(
                        "daisy.plus refused to delete key %s (HTTP %s)."
                        % (rec.key_name, resp.status_code)
                    )
            rec.write({"state": "revoked", "revoked_at": fields.Datetime.now(), "key_hash": False})
            _logger.info("Daisy Workbench key revoked: %s (record %s)", rec.key_name, rec.id)

    @api.model
    def _revoke_by_key(self, api_key):
        """Self sign-out: the workbench proves ownership by presenting its key."""
        if not api_key or not isinstance(api_key, str):
            return False
        rec = self.sudo().search(
            [("key_hash", "=", _sha256(api_key)), ("state", "=", "issued")], limit=1
        )
        if not rec:
            return False
        rec._revoke()
        return True

    def action_revoke(self):
        is_admin = self.env.user.has_group("daisydo_agents.group_daisy_admin")
        for rec in self:
            if not is_admin and rec.user_id != self.env.user:
                raise AccessError("You can only revoke your own Workbench installs.")
        self._revoke()
        return True

    @api.autovacuum
    def _gc_pending(self):
        """Drop sign-ins that were started but never redeemed."""
        self.sudo().search([
            ("state", "=", "pending"),
            ("create_date", "<", fields.Datetime.now() - timedelta(hours=1)),
        ]).unlink()


class ResUsersWorkbench(models.Model):
    _inherit = "res.users"

    def write(self, vals):
        res = super().write(vals)
        if vals.get("active") is False:
            tokens = self.env["daisy.workbench.token"].sudo().search(
                [("user_id", "in", self.ids), ("state", "=", "issued")]
            )
            try:
                tokens._revoke()
            except Exception:
                # Never block a deactivation; the records stay "Active" for an admin to retry.
                _logger.exception("Daisy Workbench revoke on deactivation failed for %s", self.ids)
        return res
