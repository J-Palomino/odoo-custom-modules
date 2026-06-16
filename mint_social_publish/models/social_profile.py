import logging

import requests

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .project_task import DEFAULT_BASE, PARAM_BASE, PARAM_KEY

_logger = logging.getLogger(__name__)


class MintSocialProfile(models.Model):
    _name = "mint.social.profile"
    _description = "posts.agency Connected Account"
    _order = "is_connected desc, name"

    name = fields.Char(string="Account", required=True, index=True)
    connected_platforms = fields.Char(
        string="Connected Platforms",
        help="Comma-separated platforms actually linked for this account on posts.agency.",
    )
    is_connected = fields.Boolean(string="Has Connected Platform", default=False)
    last_synced = fields.Datetime(string="Last Synced", readonly=True)
    active = fields.Boolean(default=True)

    _sql_constraints = [
        ("name_uniq", "unique(name)", "A posts.agency account with this name already exists."),
    ]

    @api.depends("name", "connected_platforms", "is_connected")
    def _compute_display_name(self):
        for rec in self:
            if rec.connected_platforms:
                rec.display_name = "%s (%s)" % (rec.name, rec.connected_platforms)
            elif not rec.is_connected:
                rec.display_name = _("%s — no platform connected") % (rec.name or "")
            else:
                rec.display_name = rec.name or ""

    def platform_list(self):
        self.ensure_one()
        raw = (self.connected_platforms or "").replace(";", ",")
        return [p.strip().lower() for p in raw.split(",") if p.strip()]

    # ------------------------------------------------------------------
    # sync from posts.agency
    # ------------------------------------------------------------------
    @api.model
    def sync_from_posts_agency(self, base=None, api_key=None):
        """Upsert the connected-accounts list from posts.agency.

        Returns the number of accounts returned by posts.agency. Optional
        base/api_key let the Settings button test with the typed (unsaved)
        values. Never returns or logs account identifiers.
        """
        ICP = self.env["ir.config_parameter"].sudo()
        base = (base or ICP.get_param(PARAM_BASE) or DEFAULT_BASE).strip().rstrip("/")
        api_key = (api_key or ICP.get_param(PARAM_KEY) or "").strip()
        if not api_key:
            raise UserError(
                _("Set the posts.agency API key first (Settings > posts.agency Publishing).")
            )
        try:
            resp = requests.get(
                "%s/api/uploadposts/users" % base,
                headers={"Authorization": "ApiKey %s" % api_key},
                timeout=30,
            )
        except Exception as exc:  # noqa: BLE001
            raise UserError(_("Could not reach posts.agency: %s") % exc)
        if resp.status_code != 200:
            raise UserError(
                _("posts.agency returned HTTP %s while listing accounts.") % resp.status_code
            )
        try:
            profiles = resp.json().get("profiles", [])
        except ValueError:
            profiles = []

        Profile = self.with_context(active_test=False)
        seen = []
        for p in profiles:
            username = (p.get("username") or "").strip()
            if not username:
                continue
            social = p.get("social_accounts") or {}
            linked = sorted(k for k, v in social.items() if v)
            vals = {
                "connected_platforms": ",".join(linked),
                "is_connected": bool(linked),
                "last_synced": fields.Datetime.now(),
                "active": True,
            }
            existing = Profile.search([("name", "=", username)], limit=1)
            if existing:
                existing.write(vals)
            else:
                self.create(dict(vals, name=username))
            seen.append(username)

        # archive accounts posts.agency no longer returns
        stale = Profile.search([("name", "not in", seen), ("active", "=", True)])
        if stale:
            stale.write({"active": False})
        return len(profiles)

    @api.model
    def _cron_sync_profiles(self):
        try:
            self.sync_from_posts_agency()
        except Exception:  # noqa: BLE001 - cron must not crash on a transient API error
            _logger.exception("posts.agency profile sync failed")
        return True

    def action_sync_profiles(self):
        count = self.sync_from_posts_agency()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("posts.agency"),
                "message": _("Synced %s account(s) from posts.agency.") % count,
                "type": "success",
                "next": {"type": "ir.actions.act_window_close"},
            },
        }

    # ------------------------------------------------------------------
    # connect / authenticate a social account (posts.agency OAuth)
    # ------------------------------------------------------------------
    @api.model
    def _social_api(self):
        ICP = self.env["ir.config_parameter"].sudo()
        base = (ICP.get_param(PARAM_BASE) or DEFAULT_BASE).strip().rstrip("/")
        key = (ICP.get_param(PARAM_KEY) or "").strip()
        if not key:
            raise UserError(_("Set the posts.agency API key first (Settings > posts.agency Publishing)."))
        return base, {"Authorization": "ApiKey %s" % key}

    @api.model
    def _ensure_remote_profile(self, username):
        """Create the profile on posts.agency if it doesn't exist (idempotent)."""
        base, headers = self._social_api()
        r = requests.post(
            "%s/api/uploadposts/users" % base,
            headers={**headers, "Content-Type": "application/json"},
            json={"username": username}, timeout=30,
        )
        if r.status_code not in (200, 201, 204, 409):
            raise UserError(_("posts.agency could not create the account '%(u)s' (HTTP %(s)s): %(b)s")
                            % {"u": username, "s": r.status_code, "b": r.text[:300]})

    @api.model
    def _social_connect_url(self, username):
        """Return the posts.agency OAuth URL to authenticate a social account
        to the given profile (valid 48h)."""
        base, headers = self._social_api()
        r = requests.post(
            "%s/api/uploadposts/users/generate-jwt" % base,
            headers={**headers, "Content-Type": "application/json"},
            json={"username": username}, timeout=30,
        )
        if r.status_code != 200:
            raise UserError(_("posts.agency could not create a connect link (HTTP %(s)s): %(b)s")
                            % {"s": r.status_code, "b": r.text[:300]})
        url = (r.json() or {}).get("access_url")
        if not url:
            raise UserError(_("posts.agency did not return a connect link."))
        return url

    def action_connect(self):
        """Per-account button: open the posts.agency page to connect/re-auth a platform."""
        self.ensure_one()
        if not self.env.user.has_group("base.group_system"):
            raise UserError(_("Only Settings administrators can connect posts.agency accounts."))
        return {
            "type": "ir.actions.act_url",
            "url": self._social_connect_url(self.name),
            "target": "new",
        }


class MintSocialConnectWizard(models.TransientModel):
    _name = "mint.social.connect.wizard"
    _description = "Add / Connect a posts.agency Account"

    username = fields.Char(
        string="Account Username", required=True,
        help="The posts.agency profile handle to create/connect, e.g. themintchicago. "
        "Lowercase, no spaces.",
    )

    def action_connect(self):
        """Create the profile on posts.agency if new, sync it into Odoo, and open
        the OAuth page to authenticate the social platform."""
        self.ensure_one()
        if not self.env.user.has_group("base.group_system"):
            raise UserError(_("Only Settings administrators can connect posts.agency accounts."))
        Profile = self.env["mint.social.profile"]
        username = (self.username or "").strip().lower().replace(" ", "")
        if not username:
            raise UserError(_("Enter an account username."))
        Profile._ensure_remote_profile(username)
        Profile.sync_from_posts_agency()
        return {
            "type": "ir.actions.act_url",
            "url": Profile._social_connect_url(username),
            "target": "new",
        }
