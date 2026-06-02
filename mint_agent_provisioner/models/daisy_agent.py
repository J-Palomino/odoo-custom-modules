import logging

from odoo import api, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

PARAM_AUTO = "mint_agent_provisioner.auto_provision"
PARAM_MIRROR = "mint_agent_provisioner.mirror_manager_groups"
_FALSEY = ("False", "false", "0", "no", "off", "")


def _truthy(icp, param, default="True"):
    return icp.get_param(param, default) not in _FALSEY


class DaisyAgent(models.Model):
    _inherit = "daisy.agent"

    # ------------------------------------------------------------------
    # Auto-provision on create
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        agents = super().create(vals_list)
        icp = self.env["ir.config_parameter"].sudo()
        if not _truthy(icp, PARAM_AUTO):
            return agents
        for agent in agents:
            # Provisioning must never block agent creation: a bad login,
            # missing Daisy service, etc. should leave a usable draft agent
            # plus a chatter note, not raise out of create().
            #
            # Each attempt runs in its own savepoint. _mint_odoo_api_key() uses
            # raw cr.execute(), so a failure would otherwise abort the whole
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

    def _auto_provision_identity(self):
        """Give a freshly-created agent its own Odoo user + minted API key so it
        can call Odoo/MCP immediately, with no manual hire step.

        Reuses ``daisydo_agents._mint_odoo_api_key()`` for the key so the shape
        matches the hire / wizard paths. Idempotent: if the agent already has an
        MCP key (e.g. created through the create-agent-for-user wizard) nothing
        happens.
        """
        self.ensure_one()
        if self.mcp_odoo_api_key:
            return  # already provisioned elsewhere
        if not self.code:
            raise UserError("Agent has no code; cannot derive a login/email.")

        if not self.user_id:
            self.user_id = self._provision_agent_user()

        # Direct-SQL insert in _mint_odoo_api_key() leaves expiration_date NULL,
        # so the key never expires and is not subject to the per-user 90-day cap.
        raw_key = self._mint_odoo_api_key()
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

    def _post_provision_note(self, raw_key=None, error=None):
        """Post a one-time chatter note. The raw key is unrecoverable after this
        (Odoo only stores a pbkdf2 hash), so it is surfaced here once."""
        self.ensure_one()
        from markupsafe import Markup

        if error:
            body = Markup(
                "<b>⚠️ Agent auto-provision failed</b><br/>"
                "<code>%s</code><br/>"
                "<i>Fix the cause, then use the <b>Generate Odoo API Key</b> "
                "button (or re-hire) to finish provisioning.</i>"
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
