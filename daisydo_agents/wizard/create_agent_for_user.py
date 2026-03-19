# -*- coding: utf-8 -*-
"""
Wizard: Create a Daisy Agent for an employee.

1. Admin picks a target user (the person the agent will assist)
2. Generates a random first name + target user's first name as last name
3. Creates an Odoo API key in the target user's name
4. Stores the MCP connection config (Odoo URL + API key + username)
5. Creates the daisy.agent record reporting to the target user
"""
import hashlib
import logging
import os
import random
import re
import secrets

from odoo import models, fields, api
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Random first names for agents — diverse, gender-neutral-leaning, short
AGENT_FIRST_NAMES = [
    "Ada", "Aiden", "Aria", "Atlas", "Avery", "Blake", "Briar", "Cairo",
    "Cleo", "Coda", "Cora", "Cypress", "Dana", "Delta", "Eden", "Ellis",
    "Ember", "Fern", "Finley", "Flynn", "Harbor", "Harper", "Haven", "Iris",
    "Ivy", "Jade", "Jasper", "Jules", "Kai", "Lark", "Leo", "Linden",
    "Luna", "Maple", "Marlo", "Meadow", "Milan", "Mira", "Neve", "Noa",
    "Nova", "Oakley", "Olive", "Onyx", "Orion", "Pearl", "Piper", "Quinn",
    "Raven", "Reed", "Reese", "Riley", "River", "Robin", "Rowan", "Ruby",
    "Rune", "Sage", "Salem", "Scout", "Shay", "Sienna", "Skye", "Sol",
    "Sparrow", "Sterling", "Storm", "Tatum", "Terra", "Vale", "Vesper",
    "Violet", "Wren", "Zara", "Zephyr", "Zinnia",
]


class CreateAgentForUser(models.TransientModel):
    _name = "daisy.agent.create.for.user"
    _description = "Create Agent for Employee"

    target_user_id = fields.Many2one(
        "res.users",
        string="Create Agent For",
        required=True,
        domain=[("active", "=", True), ("share", "=", False)],
        help="The employee this agent will assist. The agent reports to them.",
    )
    daisy_api_key = fields.Char(
        string="Daisy+ API Key",
        required=True,
        help="API key for the Daisy+ platform (used for AI responses)",
    )
    daisy_agency_id = fields.Char(
        string="Chatflow ID",
        required=True,
        help="Daisy+ chatflow/agency ID for this agent",
    )
    agent_first_name = fields.Char(
        string="Agent First Name",
        help="Leave blank to auto-generate a random name",
    )
    preview_name = fields.Char(
        string="Agent Name Preview",
        compute="_compute_preview_name",
    )
    ai_personality = fields.Selection(
        [
            ("professional", "Professional"),
            ("friendly", "Friendly"),
            ("technical", "Technical"),
            ("empathetic", "Empathetic"),
            ("concise", "Concise"),
        ],
        default="friendly",
        string="Personality",
    )

    @api.depends("target_user_id", "agent_first_name")
    def _compute_preview_name(self):
        for wiz in self:
            if not wiz.target_user_id:
                wiz.preview_name = ""
                continue
            first = wiz.agent_first_name or "(random)"
            last = (wiz.target_user_id.name or "").split()[0]
            wiz.preview_name = f"{first} {last}"

    def action_create_agent(self):
        self.ensure_one()
        target = self.target_user_id

        if not target.partner_id:
            raise UserError("Selected user has no partner record.")

        # --- 1. Pick agent name ---
        target_first_name = (target.name or "User").split()[0]
        if self.agent_first_name:
            first_name = self.agent_first_name.strip()
        else:
            # Pick a random name not already used by another agent with this last name
            used = set(
                self.env["daisy.agent"]
                .search([("name", "ilike", f"% {target_first_name}")])
                .mapped(lambda a: a.name.split()[0].lower())
            )
            available = [n for n in AGENT_FIRST_NAMES if n.lower() not in used]
            if not available:
                available = AGENT_FIRST_NAMES  # all used, allow duplicates
            first_name = random.choice(available)

        agent_name = f"{first_name} {target_first_name}"
        code = re.sub(r"[^a-z0-9]+", "-", agent_name.lower()).strip("-")[:30]

        # Ensure code uniqueness
        base_code = code
        suffix = 2
        while self.env["daisy.agent"].search_count([("code", "=", code)]):
            code = f"{base_code[:26]}-{suffix}"
            suffix += 1

        # --- 2. Generate Odoo API key for the target user ---
        raw_key = secrets.token_hex(30)  # 60 char hex string
        key_index = raw_key[:8]

        # Hash with passlib (Odoo's key crypt context)
        from passlib.context import CryptContext
        key_ctx = CryptContext(["pbkdf2_sha512"], pbkdf2_sha512__rounds=6000)
        key_hash = key_ctx.hash(raw_key)

        # Insert directly into res_users_apikeys
        self.env.cr.execute("""
            INSERT INTO res_users_apikeys (name, user_id, scope, index, key, create_date)
            VALUES (%s, %s, %s, %s, %s, NOW() AT TIME ZONE 'utc')
            RETURNING id
        """, [
            f"Daisy Agent: {agent_name}",
            target.id,
            "rpc",
            key_index,
            key_hash,
        ])
        apikey_id = self.env.cr.fetchone()[0]
        _logger.info(
            "Created API key (id=%s) for user %s [%s] — agent: %s",
            apikey_id, target.login, target.id, agent_name,
        )

        # --- 3. Build MCP connection config ---
        odoo_url = self.env["ir.config_parameter"].sudo().get_param(
            "web.base.url", "https://letsgomint.us"
        )
        mcp_config = {
            "odoo_url": odoo_url,
            "odoo_username": target.login,
            "odoo_api_key": raw_key,
            "mcp_server_url": "https://fastapi-mcp-production.up.railway.app",
            "mcp_sse_endpoint": "/mcp/sse",
        }

        # --- 4. Create the agent ---
        agent = self.env["daisy.agent"].create({
            "name": agent_name,
            "code": code,
            "role": f"AI Assistant to {target.name}",
            "bio": f"Personal AI assistant created for {target.name}. "
                   f"Connected to Odoo via MCP with {target.login}'s permissions.",
            "daisy_api_key": self.daisy_api_key,
            "daisy_agency_id": self.daisy_agency_id,
            "ai_personality": self.ai_personality,
            "manager_id": target.id,
            "mcp_odoo_url": mcp_config["odoo_url"],
            "mcp_odoo_username": target.login,
            "mcp_odoo_api_key": raw_key,
            "mcp_server_url": mcp_config["mcp_server_url"],
            "state": "draft",
        })

        _logger.info(
            "Created agent '%s' (id=%s) for user %s",
            agent_name, agent.id, target.login,
        )

        # Open the new agent form
        return {
            "type": "ir.actions.act_window",
            "name": agent_name,
            "res_model": "daisy.agent",
            "res_id": agent.id,
            "view_mode": "form",
            "target": "current",
        }
