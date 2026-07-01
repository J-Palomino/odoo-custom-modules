import json
import logging
import os

from odoo import models, fields, api
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

_AGENT_EMAIL_DOMAIN = os.environ.get('BRAND_AGENT_EMAIL_DOMAIN', 'daisy.plus')


class DaisyAgent(models.Model):
    _name = "daisy.agent"
    _description = "Daisy Agent"
    _order = "name"
    _inherit = ["mail.thread"]

    # --- Identity ---
    name = fields.Char(required=True, tracking=True)
    code = fields.Char(
        string="Code",
        required=True,
        help="Unique identifier, used as login/email prefix",
    )
    avatar = fields.Image(max_width=256, max_height=256)
    role = fields.Char(help="e.g. Support Agent, Sales Rep")
    bio = fields.Text()

    # --- Linked Odoo user ---
    user_id = fields.Many2one("res.users", string="Odoo User", readonly=True, ondelete="restrict")
    partner_id = fields.Many2one("res.partner", related="user_id.partner_id", string="Partner", store=True)
    user_group_ids = fields.Many2many(related="user_id.group_ids", string="User Groups", readonly=True)
    email = fields.Char(compute="_compute_email", store=True)
    manager_id = fields.Many2one(
        "res.users",
        string="Reports To",
        help="Employee this agent assists. Set by the create-agent-for-user wizard.",
    )
    x_private_owner_id = fields.Many2one(
        "res.users",
        string="Private To (owner)",
        help="When set, this agent is PRIVATE: it only auto-replies to this "
             "user's messages, and it is hidden from Discuss @mention / new-DM "
             "suggestions for everyone else. Leave empty for a normal shared agent.",
    )
    active = fields.Boolean(default=True)

    # --- MCP credentials (set by create-agent-for-user wizard) ---
    mcp_odoo_url = fields.Char(string="MCP Odoo URL")
    mcp_odoo_username = fields.Char(string="MCP Odoo Username")
    mcp_odoo_api_key = fields.Char(string="MCP Odoo API Key")
    mcp_server_url = fields.Char(string="MCP Server URL")

    # --- Daisy+ AI config ---
    daisy_api_key = fields.Char(string="Daisy+ API Key")
    daisy_agency_id = fields.Char(string="Agency ID")
    ai_system_prompt = fields.Text(string="System Prompt")
    ai_personality = fields.Selection(
        [
            ("professional", "Professional"),
            ("friendly", "Friendly"),
            ("technical", "Technical"),
            ("empathetic", "Empathetic"),
            ("concise", "Concise"),
        ],
        default="friendly",
    )
    ai_handoff_threshold = fields.Float(default=0.7, help="Confidence below this triggers human handoff (0-1)")
    ai_max_turns = fields.Integer(default=20, help="Max back-and-forth messages before auto-handoff")
    ai_greeting = fields.Char(default="Hi! I'm here to help. What can I do for you?")

    # --- Lifecycle ---
    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("active", "Active"),
            ("suspended", "Suspended"),
            ("terminated", "Terminated"),
        ],
        default="draft",
        required=True,
        tracking=True,
    )
    hire_date = fields.Datetime(readonly=True)
    termination_date = fields.Datetime(readonly=True)

    # --- Email Project ---
    mail_project_id = fields.Many2one("project.project", string="Email Project", readonly=True)

    # --- Livechat ---
    livechat_channel_ids = fields.Many2many("im_livechat.channel", string="Livechat Channels")
    max_concurrent_chats = fields.Integer(default=10)

    # --- Camera Feeds ---
    feed_ids = fields.One2many("daisy.agent.feed", "agent_id", string="Camera Feeds")
    feed_count = fields.Integer(compute="_compute_feed_count")
    has_active_feeds = fields.Boolean(compute="_compute_feed_count")

    # --- Metrics ---
    active_conversations = fields.Integer(compute="_compute_conversation_stats")
    total_conversations = fields.Integer(compute="_compute_conversation_stats")
    total_messages_sent = fields.Integer(compute="_compute_message_stats")
    metric_ids = fields.One2many("daisy.agent.metric", "agent_id", string="Daily Metrics")

    _code_unique = models.Constraint(
        "UNIQUE(code)",
        "Agent code must be unique.",
    )

    def _compute_feed_count(self):
        for agent in self:
            feeds = agent.feed_ids
            agent.feed_count = len(feeds)
            agent.has_active_feeds = any(f.go2rtc_status == "active" for f in feeds)

    @api.depends("code")
    def _compute_email(self):
        for agent in self:
            agent.email = f"{agent.code}@{_AGENT_EMAIL_DOMAIN}" if agent.code else False

    def _compute_conversation_stats(self):
        for agent in self:
            if not agent.partner_id:
                agent.active_conversations = 0
                agent.total_conversations = 0
                continue
            channels = self.env["discuss.channel"].search([
                ("channel_member_ids.partner_id", "=", agent.partner_id.id),
                ("channel_type", "=", "livechat"),
            ])
            agent.total_conversations = len(channels)
            # Active = channels with a message in the last 24 hours
            cutoff = fields.Datetime.subtract(fields.Datetime.now(), hours=24)
            active = channels.filtered(
                lambda c: c.message_ids[:1].date >= cutoff if c.message_ids else False
            )
            agent.active_conversations = len(active)

    def _compute_message_stats(self):
        for agent in self:
            if not agent.partner_id:
                agent.total_messages_sent = 0
                continue
            agent.total_messages_sent = self.env["mail.message"].search_count([
                ("author_id", "=", agent.partner_id.id),
                ("daisy_ai_generated", "=", True),
            ])

    # ---- Configuration check ----

    def _get_effective_api_key(self):
        """Return agent's API key, falling back to global system parameter."""
        self.ensure_one()
        if self.daisy_api_key:
            return self.daisy_api_key
        return self.env["ir.config_parameter"].sudo().get_param("daisy_bot.api_key", "")

    def _get_effective_agency_id(self):
        """Return agent's agency ID, falling back to global api_url."""
        self.ensure_one()
        if self.daisy_agency_id:
            return self.daisy_agency_id
        global_url = self.env["ir.config_parameter"].sudo().get_param("daisy_bot.api_url", "")
        if "/prediction/" in global_url:
            return global_url.rsplit("/prediction/", 1)[-1].strip("/")
        return ""

    @api.onchange("daisy_api_key", "daisy_agency_id")
    def _onchange_warn_missing_config(self):
        """Show inline warning when key fields are empty."""
        warnings = []
        if not self.daisy_api_key:
            global_key = self.env["ir.config_parameter"].sudo().get_param("daisy_bot.api_key", "")
            if global_key:
                warnings.append("No API key set — will use the global key from System Parameters.")
            else:
                warnings.append("No API key set and no global fallback configured.")
        if not self.daisy_agency_id:
            warnings.append("No chatflow selected — the agent won't know which AI to use.")
        if warnings:
            return {
                "warning": {
                    "title": "Incomplete Configuration",
                    "message": "\n".join(warnings),
                }
            }

    # ---- Lifecycle actions ----

    def action_hire(self):
        self.ensure_one()
        if self.state != "draft":
            raise UserError("Can only hire agents in Draft state.")
        effective_key = self._get_effective_api_key()
        if not effective_key:
            raise UserError(
                "No API key available. Set a Daisy+ API Key on this agent, "
                "or configure a global key in Settings → Technical → System Parameters "
                "(daisy_bot.api_key)."
            )

        # Validate API key
        ai_svc = self.env["daisy.ai.service"]
        result = ai_svc.validate_api_key(effective_key)
        if not result.get("valid"):
            raise UserError(f"Invalid API key: {result.get('error', 'unknown error')}")

        # Create Odoo user for the agent.
        # skip_daisy_agent_autocreate guards against the res.users.create hook
        # recursively spawning another agent for this agent's own login.
        if not self.user_id:
            user = self.env["res.users"].sudo().with_context(
                skip_daisy_agent_autocreate=True
            ).create({
                "name": self.name,
                "login": self.email,
                "email": self.email,
                "image_1920": self.avatar,
                "active": True,
                "group_ids": [(6, 0, [self.env.ref("base.group_user").id])],
            })
            self.user_id = user

        # Add agent's partner to livechat channels as operator
        if self.partner_id:
            for channel in self.livechat_channel_ids:
                if self.partner_id not in channel.user_ids.partner_id:
                    channel.sudo().write({"user_ids": [(4, self.user_id.id)]})

        # Create email project with mail alias for catchall routing
        if not self.mail_project_id:
            project = self.env["project.project"].sudo().create({
                "name": f"{self.name} - Email",
                "alias_name": self.code,
                "daisy_agent_id": self.id,
            })
            self.mail_project_id = project

        # Mint the agent's own scoped Odoo MCP key + provision on Daisy+ (best-effort).
        if not self.mcp_odoo_api_key:
            self._mint_mcp_key()
        self._provision_on_daisy()

        self.write({
            "state": "active",
            "hire_date": fields.Datetime.now(),
        })
        _logger.info("Agent %s hired", self.name)

    def action_suspend(self):
        self.ensure_one()
        if self.state != "active":
            raise UserError("Can only suspend active agents.")
        # Remove from livechat channels
        for channel in self.livechat_channel_ids:
            channel.sudo().write({"user_ids": [(3, self.user_id.id)]})
        self.state = "suspended"
        _logger.info("Agent %s suspended", self.name)

    def action_reactivate(self):
        self.ensure_one()
        if self.state != "suspended":
            raise UserError("Can only reactivate suspended agents.")
        if self.partner_id:
            for channel in self.livechat_channel_ids:
                if self.partner_id not in channel.user_ids.partner_id:
                    channel.sudo().write({"user_ids": [(4, self.user_id.id)]})
        if self.mail_project_id:
            self.mail_project_id.sudo().active = True
        self.state = "active"
        _logger.info("Agent %s reactivated", self.name)

    def action_terminate(self):
        self.ensure_one()
        if self.state in ("draft", "terminated"):
            raise UserError("Cannot terminate an agent in this state.")
        for channel in self.livechat_channel_ids:
            channel.sudo().write({"user_ids": [(3, self.user_id.id)]})
        if self.mail_project_id:
            self.mail_project_id.sudo().active = False
        if self.user_id:
            self.user_id.sudo().active = False
        self.write({
            "state": "terminated",
            "termination_date": fields.Datetime.now(),
        })
        _logger.info("Agent %s terminated", self.name)

    def action_view_conversations(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": f"{self.name} — Conversations",
            "res_model": "discuss.channel",
            "view_mode": "list,form",
            "domain": [
                ("channel_member_ids.partner_id", "=", self.partner_id.id),
                ("channel_type", "=", "livechat"),
            ],
        }

    def action_view_messages(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": f"{self.name} — AI Messages",
            "res_model": "mail.message",
            "view_mode": "list",
            "domain": [
                ("author_id", "=", self.partner_id.id),
                ("daisy_ai_generated", "=", True),
            ],
        }

    def action_view_feeds(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": f"{self.name} — Camera Feeds",
            "res_model": "daisy.agent.feed",
            "view_mode": "list,form",
            "domain": [("agent_id", "=", self.id)],
            "context": {"default_agent_id": self.id},
        }

    def action_open_user_settings(self):
        """Open the linked Odoo user form to manage permissions."""
        self.ensure_one()
        if not self.user_id:
            raise UserError("This agent has no linked Odoo user yet. Hire the agent first.")
        return {
            "type": "ir.actions.act_window",
            "name": f"{self.name} — User Settings",
            "res_model": "res.users",
            "res_id": self.user_id.id,
            "view_mode": "form",
            "target": "current",
        }

    def _enqueue_response(self, channel_model, channel_id, user_text, history, conversation_id, context_prefix="", session_id=None):
        """Create a job record and trigger immediate cron processing.

        Idempotent: if an identical job for the same target+message is still
        pending (not yet picked up by the cron), reuse it instead of creating
        a duplicate. This collapses the common sequential double-enqueue — a
        dispatch firing on both create() and write(), or an un-assign/re-assign
        churn — into a single AI reply.
        """
        self.ensure_one()
        Job = self.env["daisy.agent.job"].sudo()
        # Match on the full content of the enqueue, not just the text: the
        # genuine double-fire of one event carries identical session/context,
        # so this still collapses it, while a distinct same-text message in a
        # different session/context is NOT dropped.
        existing = Job.search(
            [
                ("agent_id", "=", self.id),
                ("channel_model", "=", channel_model),
                ("channel_id", "=", channel_id),
                ("message_text", "=", user_text),
                ("context_prefix", "=", context_prefix or False),
                ("session_id", "=", session_id or False),
                ("state", "=", "pending"),
            ],
            limit=1,
        )
        if not existing:
            Job.create({
                "agent_id": self.id,
                "channel_model": channel_model,
                "channel_id": channel_id,
                "message_text": user_text,
                "conversation_history": json.dumps(history) if history else False,
                "conversation_id": conversation_id or False,
                "context_prefix": context_prefix or False,
                "session_id": session_id or False,
            })
        self.env.ref("daisydo_agents.ir_cron_process_agent_jobs")._trigger()

    def get_ai_response(self, message, conversation_history=None, conversation_id=None, override_config=None, session_id=None):
        """Call Daisy+ API using this agent's own credentials."""
        self.ensure_one()
        ai_svc = self.env["daisy.ai.service"]
        return ai_svc._call_daisy_api_for_agent(
            self, message, conversation_history, conversation_id, override_config, session_id=session_id,
        )
