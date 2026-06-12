import logging
import secrets

from odoo import api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

PARAM_AUTO = "mint_agent_provisioner.auto_provision"
PARAM_MIRROR = "mint_agent_provisioner.mirror_manager_groups"
PARAM_MCP = "daisy.mcp_server_url"
DEFAULT_MCP = "https://fastapi-mcp-production.up.railway.app"
_FALSEY = ("False", "false", "0", "no", "off", "")

# Match daisydo_agents' own key naming so the two mint paths address the same
# row (rotation/cleanup stay consistent across module versions).
KEY_NAME_TMPL = "Daisy Agent: %s"


def _truthy(icp, param, default="True"):
    return icp.get_param(param, default) not in _FALSEY


class DaisyAgent(models.Model):
    _inherit = "daisy.agent"

    # ------------------------------------------------------------------
    # Hooks: provision on create AND ensure on hire
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        agents = super().create(vals_list)
        icp = self.env["ir.config_parameter"].sudo()
        if not _truthy(icp, PARAM_AUTO):
            return agents
        for agent in agents:
            # Each attempt runs in its own savepoint. _mint_mcp_key() uses raw
            # cr.execute(), so a failure would otherwise abort the whole
            # transaction and make the recovery message_post() fail too. The
            # savepoint rolls the failed attempt back (no orphan user/key) while
            # keeping the cursor usable for the note and the next agent.
            try:
                with self.env.cr.savepoint():
                    agent._auto_provision_identity()
            except Exception as exc:  # noqa: BLE001 - defensive on a create hook
                _logger.warning(
                    "mint_agent_provisioner: auto-provision failed for agent "
                    "%s (id=%s): %s", agent.name, agent.id, exc,
                )
                try:
                    with self.env.cr.savepoint():
                        agent._post_provision_note(error=str(exc))
                except Exception:  # noqa: BLE001
                    _logger.exception(
                        "mint_agent_provisioner: could not post failure note"
                    )
        return agents

    def action_hire(self):
        """Run the standard hire, then guarantee the agent has an MCP key.

        This is a safety net for agents created before this module existed, or
        with auto-provision disabled. It never changes hire's own outcome: if
        super() raises, that behaviour is unchanged; if a key already exists,
        provisioning is skipped.
        """
        res = super().action_hire()
        icp = self.env["ir.config_parameter"].sudo()
        if not _truthy(icp, PARAM_AUTO):
            return res
        for agent in self:
            if agent.mcp_odoo_api_key:
                continue
            try:
                with self.env.cr.savepoint():
                    agent._auto_provision_identity()
            except Exception as exc:  # noqa: BLE001
                _logger.warning(
                    "mint_agent_provisioner: hire-time provision failed for "
                    "agent %s (id=%s): %s", agent.name, agent.id, exc,
                )
        return res

    # ------------------------------------------------------------------
    # Provisioning
    # ------------------------------------------------------------------
    def _auto_provision_identity(self):
        """Give the agent its own Odoo user + minted MCP API key. Idempotent:
        if an MCP key already exists (wizard / prior run) nothing happens."""
        self.ensure_one()
        if self.mcp_odoo_api_key:
            return
        if not self.code:
            raise UserError("Agent has no code; cannot derive a login/email.")

        if not self.user_id:
            self.user_id = self._provision_agent_user()

        raw_key = self._mint_mcp_key()
        self._post_provision_note(raw_key=raw_key)

    def _provision_agent_user(self):
        """Create the dedicated res.users backing this agent.

        If the agent has a ``manager_id`` and group-mirroring is enabled, the
        new user copies that manager's groups + companies so the agent acts with
        the same permissions ("mimic their permissions"). Otherwise it is a
        plain internal user (``base.group_user``).
        """
        self.ensure_one()
        icp = self.env["ir.config_parameter"].sudo()
        base_user = self.env.ref("base.group_user")

        vals = {
            "name": self.name,
            "login": self.email,
            "email": self.email,
            "image_1920": self.avatar,
            "active": True,
            "group_ids": [(6, 0, base_user.ids)],
        }

        manager = self.manager_id
        if manager and _truthy(icp, PARAM_MIRROR):
            main_company = self.env.ref("base.main_company")
            vals["group_ids"] = [(6, 0, (manager.group_ids | base_user).ids)]
            vals["company_id"] = (manager.company_id or self.env.company).id
            vals["company_ids"] = [
                (6, 0, (manager.company_ids | main_company).ids)
            ]

        return self.env["res.users"].sudo().create(vals)

    def _mint_mcp_key(self):
        """Mint a never-expiring, unrestricted (scope ``rpc``) Odoo API key on
        the agent's user and store the raw value on the agent so the Daisy+/MCP
        stack can call Odoo as this agent.

        Self-contained on purpose: Odoo only persists a pbkdf2 hash of an API
        key, so the row must be inserted directly. Doing it here (rather than
        delegating to daisydo_agents) keeps this module working on any
        daisydo_agents version, including prod builds that predate the mint
        helper. Any prior key minted for this agent (matched by name) is rotated
        out first.
        """
        self.ensure_one()
        if not self.user_id:
            raise UserError("Agent has no linked Odoo user to mint a key for.")

        from passlib.context import CryptContext

        key_name = KEY_NAME_TMPL % self.name
        raw_key = secrets.token_hex(30)
        key_ctx = CryptContext(["pbkdf2_sha512"], pbkdf2_sha512__rounds=6000)
        key_hash = key_ctx.hash(raw_key)

        self.env.cr.execute(
            "DELETE FROM res_users_apikeys WHERE user_id = %s AND name = %s",
            [self.user_id.id, key_name],
        )
        self.env.cr.execute(
            """
            INSERT INTO res_users_apikeys (name, user_id, scope, index, key, create_date)
            VALUES (%s, %s, 'rpc', %s, %s, NOW() AT TIME ZONE 'utc')
            """,
            [key_name, self.user_id.id, raw_key[:8], key_hash],
        )

        icp = self.env["ir.config_parameter"].sudo()
        self.write({
            "mcp_odoo_url": self.mcp_odoo_url or icp.get_param("web.base.url", ""),
            "mcp_odoo_username": self.user_id.login,
            "mcp_odoo_api_key": raw_key,
            "mcp_server_url": self.mcp_server_url or icp.get_param(PARAM_MCP, DEFAULT_MCP),
        })
        _logger.info(
            "mint_agent_provisioner: minted MCP key for agent %s (user %s)",
            self.name, self.user_id.login,
        )
        return raw_key

    def _post_provision_note(self, raw_key=None, error=None):
        """Post a one-time chatter note. The raw key is unrecoverable after this
        (Odoo only stores a pbkdf2 hash), so it is surfaced here once."""
        self.ensure_one()
        from markupsafe import Markup

        if error:
            body = Markup(
                "<b>⚠️ Agent auto-provision failed</b><br/>"
                "<code>%s</code><br/>"
                "<i>Fix the cause, then re-hire to finish provisioning.</i>"
            ) % error
        else:
            body = Markup(
                "<b>Agent auto-provisioned</b><br/>"
                "Odoo user: <code>%s</code><br/>"
                "MCP key minted (scope <code>rpc</code>, no expiry) and saved to "
                "<b>MCP Connection → Odoo API Key</b>.<br/>"
                "Raw key (shown once — store it now): <code>%s</code>"
            ) % (self.user_id.login or "", raw_key or "")
        self.message_post(body=body)
