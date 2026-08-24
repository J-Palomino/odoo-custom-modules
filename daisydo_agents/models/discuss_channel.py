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

        # Private-agent gate: an agent bound to a private owner only auto-replies
        # to that owner. Anyone else's message is silently ignored (no job, no
        # reply). See x_private_owner_id on daisy.agent.
        # (no author — email/guest — is never the owner, so it fails closed)
        private_owner = agent.sudo().x_private_owner_id
        if private_owner and message.author_id != private_owner.partner_id:
            return result

        # Build conversation history from the NEWEST messages, chronological
        # (MR-954: "date asc" + limit froze history on the oldest turns once
        # a channel outgrew ai_max_turns)
        recent = self.env["daisy.agent.job"]._recent_history_messages([
            ("res_id", "=", self.id),
            ("model", "=", "discuss.channel"),
        ], agent.ai_max_turns)

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

        # Enqueue AI response job (processed by cron worker). Prefix with the
        # active user's identity so the agent knows who it is assisting.
        user_text = html2plaintext(message.body) if message.body else ""
        session_id = self._daisy_session_id_for_message(message)
        agent._enqueue_response(
            "discuss.channel", self.id, user_text, history, conversation_id,
            context_prefix=self._daisy_requester_context(message),
            session_id=session_id,
        )

        return result

    def _daisy_session_id_for_message(self, message):
        """Stable per-user key for Daisy+ sessionId: partner → guest → channel uuid."""
        self.ensure_one()
        if message.author_id:
            return f"partner-{message.author_id.id}"
        guest = getattr(message, "author_guest_id", False)
        if guest:
            return f"guest-{guest.id}"
        uuid = getattr(self, "uuid", False)
        return f"channel-{uuid or self.id}"
