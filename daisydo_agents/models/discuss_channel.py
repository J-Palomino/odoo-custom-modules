import logging
from odoo import models
from odoo.tools import html2plaintext

_logger = logging.getLogger(__name__)


class DiscussChannel(models.Model):
    _inherit = "discuss.channel"

    def _message_post_after_hook(self, message, msg_vals):
        """Auto-respond with AI when a visitor messages an agent."""
        result = super()._message_post_after_hook(message, msg_vals)

        if self.channel_type not in ("livechat", "chat"):
            return result

        # Find the agent based on channel type
        if self.channel_type == "livechat":
            operator_partner = self.livechat_operator_id
            if not operator_partner:
                return result
            agent = self.env["daisy.agent"].sudo().search([
                ("partner_id", "=", operator_partner.id),
                ("state", "=", "active"),
            ], limit=1)
        else:
            # DM: find which channel member is an agent
            agent = None
            for member in self.channel_member_ids:
                agent = self.env["daisy.agent"].sudo().search([
                    ("partner_id", "=", member.partner_id.id),
                    ("state", "=", "active"),
                ], limit=1)
                if agent:
                    operator_partner = member.partner_id
                    break
            if not agent:
                return result

        # Don't respond to our own messages (infinite loop prevention)
        if message.author_id == operator_partner:
            return result

        # Skip AI-generated messages to prevent response loops
        if getattr(message, 'daisy_ai_generated', False):
            return result

        if not agent:
            return result

        # Build conversation history from recent messages
        recent = self.env["mail.message"].search([
            ("res_id", "=", self.id),
            ("model", "=", "discuss.channel"),
        ], order="date asc", limit=agent.ai_max_turns)

        history = []
        for msg in recent:
            role = "assistant" if msg.author_id == operator_partner else "user"
            if msg.body:
                history.append({"role": role, "content": html2plaintext(msg.body)})

        # Get last conversation_id from previous AI messages
        last_ai_msg = self.env["mail.message"].search([
            ("res_id", "=", self.id),
            ("model", "=", "discuss.channel"),
            ("daisy_conversation_id", "!=", False),
        ], order="date desc", limit=1)
        conversation_id = last_ai_msg.daisy_conversation_id if last_ai_msg else None

        # Enqueue AI response job (processed by cron worker)
        user_text = html2plaintext(message.body) if message.body else ""
        agent._enqueue_response(
            "discuss.channel", self.id, user_text, history, conversation_id,
        )

        return result
