import logging
import os

import requests
from odoo import models, fields, api
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

_BRAND_NAME = os.environ.get('BRAND_NAME', 'Mint')
_PRIMARY_COLOR = os.environ.get('BRAND_PRIMARY_COLOR', '#00954c')
_LIVECHAT_GREETING = os.environ.get(
    'LIVECHAT_GREETING',
    "Hi! 👋 How can I help you today?",
)


class DaisyChatChannel(models.Model):
    """Extended livechat channel with Daisy+ AI features."""
    _inherit = "im_livechat.channel"

    # Daisy+ API Configuration
    daisy_api_key = fields.Char(
        string="AI API Key",
        help="Your API key for AI-powered chat responses",
    )
    daisy_agency_id = fields.Char(
        string="Chatflow",
        help="Select your Daisy+ chatflow"
    )
    daisy_agency_name = fields.Char(
        string="Agency Name",
        compute="_compute_agency_name",
        store=True
    )
    daisy_available_agencies = fields.Text(
        string="Available Agencies",
        help="JSON list of available agencies for this API key"
    )

    # AI Settings
    ai_enabled = fields.Boolean(
        string="Enable Daisy+ AI",
        default=True,
        help="Enable Daisy+ AI to handle conversations"
    )
    ai_system_prompt = fields.Text(
        string="Custom Instructions",
        help="Additional instructions for the AI assistant"
    )
    ai_handoff_threshold = fields.Float(
        string="Human Handoff Threshold",
        default=0.7,
        help="Confidence threshold below which to escalate to human (0-1)"
    )

    # Auto-greeting
    auto_greet_enabled = fields.Boolean(
        string="Auto Greeting",
        default=True
    )
    auto_greet_delay = fields.Integer(
        string="Greeting Delay (seconds)",
        default=3,
        help="Seconds to wait before showing auto-greeting"
    )
    auto_greet_message = fields.Text(
        string="Greeting Message",
        default=_LIVECHAT_GREETING,
    )

    # Widget Customization
    widget_position = fields.Selection([
        ("bottom_right", "Bottom Right"),
        ("bottom_left", "Bottom Left"),
    ], string="Widget Position", default="bottom_right")
    widget_primary_color = fields.Char(
        string="Primary Color",
        default=_PRIMARY_COLOR,
    )
    widget_header_text = fields.Char(
        string="Header Text",
        default=f"{_BRAND_NAME} Chat",
    )
    widget_placeholder = fields.Char(
        string="Input Placeholder",
        default="Type your message..."
    )

    # Analytics
    total_ai_conversations = fields.Integer(
        string="AI Conversations",
        compute="_compute_ai_stats"
    )
    ai_resolution_rate = fields.Float(
        string="AI Resolution Rate",
        compute="_compute_ai_stats"
    )

    @api.depends("daisy_agency_id", "daisy_available_agencies")
    def _compute_agency_name(self):
        import json
        for channel in self:
            channel.daisy_agency_name = ""
            if channel.daisy_agency_id and channel.daisy_available_agencies:
                try:
                    agencies = json.loads(channel.daisy_available_agencies)
                    for agency in agencies:
                        if str(agency.get("id")) == str(channel.daisy_agency_id):
                            channel.daisy_agency_name = agency.get("name", "")
                            break
                except (json.JSONDecodeError, TypeError):
                    pass

    @api.depends("channel_ids")
    def _compute_ai_stats(self):
        for channel in self:
            # Placeholder for analytics computation
            channel.total_ai_conversations = 0
            channel.ai_resolution_rate = 0.0

    @api.onchange("ai_enabled", "daisy_api_key", "daisy_agency_id")
    def _onchange_warn_missing_config(self):
        """Show inline warning when AI is enabled but credentials are missing."""
        if not self.ai_enabled:
            return
        warnings = []
        if not self.daisy_api_key:
            global_key = self.env["ir.config_parameter"].sudo().get_param("daisy_bot.api_key", "")
            if global_key:
                warnings.append("No API key set — will use the global key from System Parameters.")
            else:
                warnings.append(
                    "No API key set and no global fallback configured. "
                    "AI responses will not work until a key is provided."
                )
        if not self.daisy_agency_id:
            warnings.append("No chatflow selected — the channel won't know which AI to use.")
        if warnings:
            return {
                "warning": {
                    "title": "Incomplete AI Configuration",
                    "message": "\n".join(warnings),
                }
            }

    @api.onchange("daisy_api_key")
    def _onchange_daisy_api_key(self):
        """Auto-fetch agencies when API key is entered or changed."""
        if not self.daisy_api_key:
            self.daisy_available_agencies = False
            self.daisy_agency_id = False
            return

        try:
            base_url = self.env["daisy.ai.service"]._get_daisy_api_base()
            response = requests.get(
                f"{base_url}/chatflows",
                headers={
                    "Authorization": f"Bearer {self.daisy_api_key}",
                    "Content-Type": "application/json",
                },
                timeout=10
            )
            response.raise_for_status()
            data = response.json()

            # Server returns array of chatflow objects directly
            if isinstance(data, list):
                agencies = data
            else:
                agencies = data.get("agencies", data.get("data", []))

            import json
            self.daisy_available_agencies = json.dumps(agencies)

            # Auto-select first agency if only one available
            if len(agencies) == 1:
                self.daisy_agency_id = str(agencies[0].get("id"))

        except requests.exceptions.RequestException as e:
            _logger.error(f"Daisy+ API error: {e}")
            self.daisy_available_agencies = False
            return {
                "warning": {
                    "title": "Connection Error",
                    "message": f"Could not connect to Daisy+ API: {str(e)}",
                }
            }

    def get_agency_selection(self):
        """Return list of agencies for selection widget."""
        self.ensure_one()
        import json

        if not self.daisy_available_agencies:
            return []

        try:
            agencies = json.loads(self.daisy_available_agencies)
            return [(str(a.get("id")), a.get("name", f"Agency {a.get('id')}")) for a in agencies]
        except (json.JSONDecodeError, TypeError):
            return []


class DaisyChatMessage(models.Model):
    """Extended chat message with AI metadata."""
    _inherit = "mail.message"

    daisy_ai_generated = fields.Boolean(
        string="AI Generated",
        default=False,
        help="Message was generated by Daisy+ AI"
    )
    daisy_ai_confidence = fields.Float(
        string="AI Confidence",
        help="Confidence score of AI response (0-1)"
    )
    daisy_intent = fields.Char(
        string="Detected Intent",
        help="Intent detected by AI from user message"
    )
    daisy_conversation_id = fields.Char(
        string="Daisy+ Conversation ID",
        help="Conversation ID from Daisy+ API"
    )
