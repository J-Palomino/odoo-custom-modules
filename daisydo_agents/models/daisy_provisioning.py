# -*- coding: utf-8 -*-
"""
Odoo -> Daisy+ auto-provisioning.

When a new internal Odoo user is created (and `daisy.autocreate_agents` is
enabled), spin up a matching daisy.agent and provision it on Daisy+:

  1. Clone a designated template chatflow  (daisy.template_chatflow_id)
  2. Mint a Daisy+ API key and link it to the new chatflow  (the agent's
     "secret key" — what callers use to hit its prediction endpoint)
  3. Mint a scoped Odoo XML-RPC key under the agent's OWN user so the agent
     acts in Odoo with exactly that user's permissions (the MCP credential)

All Daisy+ calls are best-effort: a failure is recorded on the agent
(`provision_state` / `provision_error`) and NEVER blocks Odoo user creation.

Config (ir.config_parameter):
  daisy.autocreate_agents   "True" to auto-create an agent per new internal user (default off)
  daisy.global_api_key      Daisy+ workspace key used for provisioning (falls back to daisy_bot.api_key)
  daisy.template_chatflow_id Chatflow id to clone for new agents (REQUIRED for provisioning)
  daisy.default_model       Model id for cloned agents (default "gpt-4o-mini")
  daisy.mcp_server_url       MCP server URL (default fastapi-mcp-production.up.railway.app)

The template chatflow's flowData may contain the placeholder tokens
__ODOO_URL__, __ODOO_USERNAME__, __ODOO_API_KEY__, __MCP_SERVER_URL__ which are
substituted with the new agent's MCP credentials at clone time.
"""
import json
import logging
import re
import secrets

import requests
from passlib.context import CryptContext

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError

# Agent chat-identity naming pattern.  Each agent gets its OWN dedicated Odoo
# user (separate from the human it mirrors) so it cannot hijack the human's
# Discuss DMs / livechat identity.  Login = "<firstname>daisy@letsgomint.us",
# display name = "<First> Daisy".  The "*daisy@letsgomint.us" namespace is
# RESERVED — a human user can never be created into it (see ResUsers below).
_AGENT_LOGIN_DOMAIN = "letsgomint.us"
_AGENT_LOGIN_SUFFIX = "daisy@" + _AGENT_LOGIN_DOMAIN  # reserved tail

_logger = logging.getLogger(__name__)

_MCP_PLACEHOLDERS = {
    "odoo_url": "__ODOO_URL__",
    "odoo_username": "__ODOO_USERNAME__",
    "odoo_api_key": "__ODOO_API_KEY__",
    "mcp_server_url": "__MCP_SERVER_URL__",
}
_DEFAULT_MCP_SERVER = "https://fastapi-mcp-production.up.railway.app"


class DaisyAIServiceProvisioning(models.AbstractModel):
    """Daisy+ chatflow + API-key CRUD used for provisioning."""
    _inherit = "daisy.ai.service"

    @api.model
    def _daisy_headers(self, api_key):
        return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    @api.model
    def get_chatflow(self, api_key, chatflow_id):
        base_url = self._get_daisy_api_base()
        resp = requests.get(
            f"{base_url}/chatflows/{chatflow_id}",
            headers=self._daisy_headers(api_key), timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    @api.model
    def create_chatflow(self, api_key, name, flow_data_json, chatflow_type="AGENTFLOW", deployed=True):
        base_url = self._get_daisy_api_base()
        resp = requests.post(
            f"{base_url}/chatflows",
            json={
                "name": name,
                "flowData": flow_data_json,
                "type": chatflow_type,
                "deployed": deployed,
            },
            headers=self._daisy_headers(api_key), timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    @api.model
    def update_chatflow(self, api_key, chatflow_id, updates):
        base_url = self._get_daisy_api_base()
        resp = requests.put(
            f"{base_url}/chatflows/{chatflow_id}",
            json=updates, headers=self._daisy_headers(api_key), timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    @api.model
    def clone_chatflow(self, api_key, template_id, name, mcp_credentials=None, model=None):
        """Clone a template chatflow into a new deployed AGENTFLOW.

        Substitutes MCP credential placeholders and (best-effort) swaps the
        model identifier. Returns the new chatflow id.
        """
        src = self.get_chatflow(api_key, template_id)
        flow_str = src.get("flowData") or "{}"

        if mcp_credentials:
            for field, token in _MCP_PLACEHOLDERS.items():
                val = mcp_credentials.get(field)
                if val:
                    flow_str = flow_str.replace(token, val)

        if model:
            target = model if model.startswith("chatDaisy:") else f"chatDaisy:{model}"

            def _swap(obj):
                if isinstance(obj, dict):
                    for k, v in list(obj.items()):
                        if isinstance(v, str) and v.startswith("chatDaisy:"):
                            obj[k] = target
                        else:
                            _swap(v)
                elif isinstance(obj, list):
                    for item in obj:
                        _swap(item)

            try:
                flow = json.loads(flow_str)
                _swap(flow)
                flow_str = json.dumps(flow)
            except (ValueError, TypeError):
                _logger.warning("clone_chatflow: could not parse flowData for model swap; keeping template model")

        created = self.create_chatflow(api_key, name, flow_str, chatflow_type="AGENTFLOW", deployed=True)
        return created.get("id")

    @api.model
    def create_apikey(self, api_key, key_name):
        """Create a Daisy+ workspace API key. Returns the created key dict {id, apiKey, ...}.

        Daisy+ `POST /apikey` responds with the FULL list of workspace keys (not
        just the created one), so pick out the entry matching key_name.
        """
        base_url = self._get_daisy_api_base()
        resp = requests.post(
            f"{base_url}/apikey",
            json={"keyName": key_name},
            headers=self._daisy_headers(api_key), timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            matches = [k for k in data if k.get("keyName") == key_name]
            return matches[-1] if matches else (data[-1] if data else {})
        return data

    @api.model
    def link_apikey(self, api_key, chatflow_id, key_id):
        """Gate a chatflow behind a specific API key."""
        return self.update_chatflow(api_key, chatflow_id, {"apikeyid": key_id})


class DaisyAgentProvisioning(models.Model):
    _inherit = "daisy.agent"

    provision_state = fields.Selection(
        [
            ("none", "Not Provisioned"),
            ("done", "Provisioned"),
            ("error", "Error"),
        ],
        default="none",
        string="Daisy+ Provisioning",
        tracking=True,
    )
    provision_error = fields.Text(string="Provisioning Error", readonly=True)
    source_user_id = fields.Many2one(
        "res.users",
        string="Mirrors User",
        readonly=True,
        ondelete="set null",
        help="Human user whose Odoo permissions this agent mirrors. The agent "
        "runs as its OWN dedicated user (user_id) whose group/company access is "
        "snapshot-copied from this user at provisioning time.",
    )

    # ---- helpers ----

    @api.model
    def _workspace_key(self):
        ICP = self.env["ir.config_parameter"].sudo()
        return (ICP.get_param("daisy.global_api_key", "") or ICP.get_param("daisy_bot.api_key", "")).strip()

    def _set_provision_error(self, message):
        self.ensure_one()
        self.write({"provision_state": "error", "provision_error": message})
        return False

    def _mint_mcp_key(self):
        """Mint a scoped Odoo RPC key under THIS agent's own user.

        The raw key is the agent's MCP credential; it grants exactly the
        agent user's Odoo permissions. Stored on mcp_odoo_api_key.
        """
        self.ensure_one()
        if not self.user_id:
            return False
        raw_key = secrets.token_hex(30)
        key_ctx = CryptContext(["pbkdf2_sha512"], pbkdf2_sha512__rounds=6000)
        key_hash = key_ctx.hash(raw_key)
        self.env.cr.execute(
            """
            INSERT INTO res_users_apikeys (name, user_id, scope, index, key, create_date)
            VALUES (%s, %s, %s, %s, %s, NOW() AT TIME ZONE 'utc')
            RETURNING id
            """,
            [f"Daisy Agent MCP: {self.name}", self.user_id.id, "rpc", raw_key[:8], key_hash],
        )
        self.env.cr.fetchone()
        ICP = self.env["ir.config_parameter"].sudo()
        self.write({
            "mcp_odoo_url": ICP.get_param("web.base.url", "https://letsgomint.us"),
            "mcp_odoo_username": self.user_id.login,
            "mcp_odoo_api_key": raw_key,
            "mcp_server_url": ICP.get_param("daisy.mcp_server_url", _DEFAULT_MCP_SERVER),
        })
        _logger.info("Minted MCP RPC key for agent %s (user %s)", self.name, self.user_id.login)
        return raw_key

    def _provision_on_daisy(self):
        """Create the agent's chatflow + linked API key on Daisy+. Best-effort."""
        self.ensure_one()
        ICP = self.env["ir.config_parameter"].sudo()
        api_key = self._workspace_key()
        template_id = ICP.get_param("daisy.template_chatflow_id", "").strip()
        if not api_key:
            return self._set_provision_error(
                "No Daisy+ workspace key (set daisy.global_api_key or daisy_bot.api_key)."
            )
        if not template_id:
            return self._set_provision_error(
                "No template chatflow set (daisy.template_chatflow_id)."
            )
        ai = self.env["daisy.ai.service"]
        model = ICP.get_param("daisy.default_model", "gpt-4o-mini")
        mcp = {
            "odoo_url": self.mcp_odoo_url or ICP.get_param("web.base.url", ""),
            "odoo_username": self.mcp_odoo_username or (self.user_id.login if self.user_id else ""),
            "odoo_api_key": self.mcp_odoo_api_key or "",
            "mcp_server_url": self.mcp_server_url or _DEFAULT_MCP_SERVER,
        }
        try:
            chatflow_id = ai.clone_chatflow(api_key, template_id, self.name, mcp_credentials=mcp, model=model)
            if not chatflow_id:
                return self._set_provision_error("clone_chatflow returned no id.")
            key_info = ai.create_apikey(api_key, f"agent:{self.code}")
            new_key = key_info.get("apiKey")
            key_id = key_info.get("id")
            if key_id:
                ai.link_apikey(api_key, chatflow_id, key_id)
            self.write({
                "daisy_agency_id": chatflow_id,
                "daisy_api_key": new_key or self.daisy_api_key,
                "provision_state": "done",
                "provision_error": False,
            })
            _logger.info("Provisioned Daisy+ agent %s: chatflow=%s key=%s", self.name, chatflow_id, key_id)
            return True
        except (requests.exceptions.RequestException, UserError, ValueError) as e:
            _logger.exception("Daisy+ provisioning failed for agent %s", self.name)
            return self._set_provision_error(str(e))

    @api.model
    def _derive_code(self, user):
        """Unique agent code from a user's login local-part."""
        import re
        base = (user.login or "agent").split("@")[0]
        code = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")[:30] or "agent"
        candidate, suffix = code, 2
        while self.search_count([("code", "=", candidate)]):
            candidate = f"{code[:26]}-{suffix}"
            suffix += 1
        return candidate

    @api.model
    def _derive_agent_login(self, source_user):
        """Unique agent login of the form '<firstname>daisy@letsgomint.us'.

        Collisions (two people sharing a first name) get a numeric suffix:
        ashtondaisy@ -> ashtondaisy2@ -> ashtondaisy3@ ...
        """
        first = (source_user.name or source_user.login or "agent").strip().split()
        base = re.sub(r"[^a-z0-9]+", "", (first[0] if first else "agent").lower()) or "agent"
        Users = self.env["res.users"].sudo().with_context(active_test=False)
        candidate = f"{base}{_AGENT_LOGIN_SUFFIX}"
        n = 2
        while Users.search_count([("login", "=", candidate)]):
            candidate = f"{base}daisy{n}@{_AGENT_LOGIN_DOMAIN}"
            n += 1
        return candidate

    @api.model
    def _create_agent_user(self, source_user):
        """Create the agent's OWN internal user, snapshot-copying the source
        human's group + company access (so the agent mirrors their permissions
        without sharing their identity)."""
        first = (source_user.name or "").strip().split()
        display = f"{(first[0] if first else source_user.login.split('@')[0]).title()} Daisy"
        login = self._derive_agent_login(source_user)
        return self.env["res.users"].sudo().with_context(
            creating_daisy_agent_user=True,   # bypasses the reserved-login guard
            skip_daisy_agent_autocreate=True,  # no recursive provisioning
            no_reset_password=True,            # no invite email
        ).create({
            "name": display,
            "login": login,
            "email": login,
            "company_id": source_user.company_id.id,
            "company_ids": [(6, 0, source_user.company_ids.ids)],
            "group_ids": [(6, 0, source_user.group_ids.ids)],  # snapshot copy
            "share": False,
            "active": True,
        })

    @api.model
    def _autocreate_for_user(self, source_user):
        """Create + provision an agent for an existing internal (human) user.

        The agent gets its OWN dedicated res.users (see _create_agent_user) so
        its Discuss/livechat identity never collides with the human's — no DM
        hijacking.  ``source_user_id`` records the human it mirrors.
        """
        if source_user.share or not source_user.active:
            return False
        if self.search_count([("source_user_id", "=", source_user.id)]):
            return False
        agent_user = self._create_agent_user(source_user)
        agent = self.create({
            "name": agent_user.name,                 # "Ashton Daisy"
            "code": self._derive_code(source_user),  # "abrackens"
            "role": "AI Agent",
            "user_id": agent_user.id,                # dedicated identity
            "source_user_id": source_user.id,        # human whose perms are mirrored
            "manager_id": source_user.id,            # "assists" this human
            "state": "active",
            "hire_date": fields.Datetime.now(),
        })
        if not agent.mail_project_id:
            try:
                project = self.env["project.project"].sudo().create({
                    "name": f"{agent.name} - Email",
                    "alias_name": agent.code,
                    # The HUMAN owns + can see the agent's inbox (invited-only).
                    "user_id": source_user.id,
                    "privacy_visibility": "followers",
                })
                agent.mail_project_id = project
                if source_user.partner_id:
                    project.message_subscribe(partner_ids=[source_user.partner_id.id])
            except Exception:
                _logger.exception("Email project creation failed for agent %s", agent.name)
        agent._mint_mcp_key()
        agent._provision_on_daisy()
        return agent

    def action_reprovision(self):
        """Manual re-run of Daisy+ provisioning (and MCP key if missing)."""
        for agent in self:
            if not agent.mcp_odoo_api_key and agent.user_id:
                agent._mint_mcp_key()
            agent._provision_on_daisy()
        return True

    def action_migrate_to_dedicated_user(self):
        """One-shot migration for legacy agents that were bound to the human's
        OWN user (pre-naming-pattern).  Spins up the dedicated
        '<firstname>daisy@' user, repoints the agent at it, revokes the old MCP
        key minted under the human, and re-provisions.

        Idempotent: agents that already have ``source_user_id`` are skipped.
        """
        migrated = self.env["daisy.agent"]
        for agent in self:
            human = agent.user_id
            if not human or agent.source_user_id:
                continue
            # Revoke the old MCP key that was minted under the human's account,
            # matched by its public index (first 8 chars of the stored raw key).
            if agent.mcp_odoo_api_key:
                self.env.cr.execute(
                    "DELETE FROM res_users_apikeys WHERE user_id=%s AND index=%s AND scope='rpc'",
                    [human.id, agent.mcp_odoo_api_key[:8]],
                )
            agent_user = self._create_agent_user(human)
            agent.write({
                "name": agent_user.name,
                "user_id": agent_user.id,
                "source_user_id": human.id,
                "manager_id": human.id,
                "mcp_odoo_api_key": False,  # force a fresh mint under the new user
            })
            agent._mint_mcp_key()
            agent._provision_on_daisy()
            migrated |= agent
            _logger.info(
                "Migrated agent %s -> dedicated user %s (mirrors %s)",
                agent.code, agent_user.login, human.login,
            )
        return migrated

    @api.model
    def provision_for_user_id(self, user_id):
        """Public RPC entry point: provision (or fetch) the agent for a human
        user id.  Returns the agent id (or False)."""
        user = self.env["res.users"].sudo().browse(int(user_id))
        if not user.exists():
            return False
        existing = self.search([("source_user_id", "=", user.id)], limit=1)
        if existing:
            return existing.id
        agent = self._autocreate_for_user(user)
        return agent.id if agent else False

    @api.model
    def _autocreate_enabled(self):
        return self.env["ir.config_parameter"].sudo().get_param(
            "daisy.autocreate_agents", "False"
        ) in ("True", "true", "1")

    @api.model
    def _provision_user_if_eligible(self, user):
        """Auto-provision an agent for a user IFF it is a real employee.

        Eligibility: internal (non-share), active, linked to an hr.employee,
        and not already backing an agent. Service/integration users (no
        hr.employee) are skipped so they don't spawn agents/chatflows/keys.
        """
        if not user or user.share or not user.active:
            return False
        if self.search_count([("source_user_id", "=", user.id)]):
            return False
        if not self.env["hr.employee"].sudo().search_count([("user_id", "=", user.id)]):
            return False
        return self._autocreate_for_user(user)


class ResUsers(models.Model):
    _inherit = "res.users"

    @api.constrains("login")
    def _check_reserved_daisy_login(self):
        """Reserve the '*daisy@letsgomint.us' namespace for Daisy AI agents so a
        human can never be created/renamed into an agent's chat identity."""
        if self.env.context.get("creating_daisy_agent_user"):
            return
        Agent = self.env["daisy.agent"].sudo()
        for user in self:
            login = (user.login or "").lower()
            if not login.endswith(_AGENT_LOGIN_SUFFIX):
                continue
            # Allow it only if this user already backs a daisy.agent.
            if not Agent.search_count([("user_id", "=", user.id)]):
                raise ValidationError(
                    "The '*%s' login namespace is reserved for Daisy AI agents."
                    % _AGENT_LOGIN_SUFFIX
                )

    @api.model_create_multi
    def create(self, vals_list):
        users = super().create(vals_list)
        if self.env.context.get("skip_daisy_agent_autocreate"):
            return users
        Agent = self.env["daisy.agent"].sudo()
        if not Agent._autocreate_enabled():
            return users
        for user in users:
            try:
                # Only provisions if the user is already linked to an hr.employee;
                # user-first onboarding is caught later by the hr.employee hook.
                Agent._provision_user_if_eligible(user)
            except Exception:
                # Never let provisioning block user creation.
                _logger.exception("Daisy auto-provision failed for user %s", user.login)
        return users


class HrEmployee(models.Model):
    _inherit = "hr.employee"

    @api.model_create_multi
    def create(self, vals_list):
        employees = super().create(vals_list)
        employees._daisy_autoprovision()
        return employees

    def write(self, vals):
        res = super().write(vals)
        if "user_id" in vals:
            self._daisy_autoprovision()
        return res

    def _daisy_autoprovision(self):
        """Provision agents for employees that have a linked internal user.

        Catches user-first onboarding (where the user existed before the
        employee link, so the res.users hook skipped it).
        """
        if self.env.context.get("skip_daisy_agent_autocreate"):
            return
        Agent = self.env["daisy.agent"].sudo()
        if not Agent._autocreate_enabled():
            return
        for emp in self:
            if not emp.user_id:
                continue
            try:
                Agent._provision_user_if_eligible(emp.user_id)
            except Exception:
                _logger.exception(
                    "Daisy auto-provision (employee link) failed for %s", emp.user_id.login
                )
