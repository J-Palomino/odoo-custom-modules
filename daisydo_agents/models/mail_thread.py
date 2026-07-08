import logging
from odoo import models
from odoo.tools import html2plaintext

_logger = logging.getLogger(__name__)


class MailThread(models.AbstractModel):
    _inherit = "mail.thread"

    def _message_post_after_hook(self, message, msg_vals):
        """Auto-respond with AI when a document follower is an agent."""
        result = super()._message_post_after_hook(message, msg_vals)

        # Skip discuss.channel — handled by discuss_channel.py
        if self._name == "discuss.channel":
            return result

        # Only respond to actual user comments, not tracking/system messages
        if message.message_type not in ("comment", "email"):
            return result

        # Find agent among followers
        agent = None
        operator_partner = None
        for follower in self.message_follower_ids:
            agent = self.env["daisy.agent"].sudo().search([
                ("partner_id", "=", follower.partner_id.id),
                ("state", "=", "active"),
            ], limit=1)
            if agent:
                operator_partner = follower.partner_id
                break

        if not agent:
            return result

        # Don't respond to our own messages (infinite loop prevention)
        if message.author_id == operator_partner:
            return result

        # Skip AI-generated messages to prevent response loops
        if getattr(message, 'daisy_ai_generated', False):
            return result

        # Private-agent gate: a private agent only auto-replies to its owner.
        # (no author — email/guest — is never the owner, so it fails closed)
        private_owner = agent.sudo().x_private_owner_id
        if private_owner and message.author_id != private_owner.partner_id:
            return result

        # Build conversation history from the NEWEST messages, chronological
        # (MR-954: "date asc" + limit froze history on the oldest turns once
        # a thread outgrew ai_max_turns)
        recent = self.env["daisy.agent.job"]._recent_history_messages([
            ("res_id", "=", self.id),
            ("model", "=", self._name),
            ("message_type", "in", ("comment", "email")),
        ], agent.ai_max_turns)

        history = []
        for msg in recent:
            role = "assistant" if msg.author_id == operator_partner else "user"
            if msg.body:
                history.append({"role": role, "content": html2plaintext(msg.body)})

        # Get last conversation_id from previous AI messages
        last_ai_msg = self.env["mail.message"].search([
            ("res_id", "=", self.id),
            ("model", "=", self._name),
            ("daisy_conversation_id", "!=", False),
        ], order="date desc", limit=1)
        conversation_id = last_ai_msg.daisy_conversation_id if last_ai_msg else None

        # Build document context prefix for the AI
        # (Flowise Agentflow V2 doesn't support overrideConfig.startState)
        doc_desc = ""
        for field_name in ("description", "note", "comment"):
            if field_name in self._fields and self[field_name]:
                doc_desc = html2plaintext(str(self[field_name]))[:500]
                break

        model_label = self.env["ir.model"]._get(self._name).name or self._name
        context_prefix = f"[Document: {model_label} #{self.id} \"{self.display_name or ''}\""
        if doc_desc:
            context_prefix += f" | {doc_desc}"
        context_prefix += "]\n\n"

        # Enqueue AI response job (processed by cron worker)
        user_text = html2plaintext(message.body) if message.body else ""
        if message.author_id:
            session_id = f"partner-{message.author_id.id}"
        else:
            # Email-only author with no partner — scope memory to the document thread
            session_id = f"thread-{self._name}-{self.id}"
        agent._enqueue_response(
            self._name, self.id, user_text, history, conversation_id,
            context_prefix=context_prefix,
            session_id=session_id,
        )

        return result
