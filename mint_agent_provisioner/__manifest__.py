{
    "name": "Mint Agent Provisioner",
    "summary": "Auto-provision a dedicated Odoo user + API key for every Daisy "
               "agent the moment it is created — no manual hire step.",
    "description": """
Mint Agent Provisioner
======================

Makes Daisy agents *dynamic*: when a ``daisy.agent`` record is created (and
again, as a safety net, on hire) this module automatically

1. creates a dedicated ``res.users`` for the agent (optionally mirroring the
   groups + companies of its ``manager_id`` so the agent "inherits their
   permissions"), and
2. mints a never-expiring Odoo API key (scope ``rpc``) on that user, stored on
   the agent's ``mcp_odoo_api_key`` so the Daisy+/FastAPI-MCP stack can call
   Odoo *as that agent* immediately.

Self-contained: the key is minted with a direct SQL insert (Odoo only persists
a pbkdf2 hash of API keys), so this works on any ``daisydo_agents`` version,
including prod builds that predate the in-module mint helper.

Idempotent: if the agent already has an MCP key (e.g. created via the
create-agent-for-user wizard) provisioning is skipped. A failure never blocks
agent creation or hire — it is logged and noted in the chatter instead.

System parameters
-----------------
* ``mint_agent_provisioner.auto_provision`` (default ``True``) — master switch.
* ``mint_agent_provisioner.mirror_manager_groups`` (default ``True``) — when an
  agent has a ``manager_id``, copy that user's groups/companies to the agent
  user.
""",
    "version": "19.0.1.2.0",
    "category": "Productivity/Daisy",
    "author": "Mint Cannabis",
    "website": "https://letsgomint.us",
    "license": "LGPL-3",
    "depends": ["daisydo_agents"],
    "data": [],
    "installable": True,
    "application": False,
    "auto_install": False,
}
