import logging
from odoo import models
from odoo.tools import html2plaintext

_logger = logging.getLogger(__name__)


class MailThread(models.AbstractModel):
    _inherit = "mail.thread"

    def _daisy_requester_context(self, message):
        """One-line identity of the person the agent is replying to (the active
        user), so the agent can address them by name and act on their behalf.
        Inherited by discuss.channel too. Returns '' when the author is unknown."""
        partner = message.author_id
        if not partner:
            return ""
        user = partner.user_ids[:1]
        who = partner.name or (user.name if user else "") or "a user"
        login = (user.login if user else "") or partner.email or ""
        ctx = f"[You are assisting: {who}"
        if login:
            ctx += f" <{login}>"
        if user:
            ctx += f" — internal Odoo user (company: {user.company_id.name or 'n/a'})"
        else:
            ctx += " — external contact (no Odoo login)"
        ctx += ". Address them by name and act on their behalf, within what they "
        ctx += "are permitted to see.]\n"
        return ctx

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

        # Build conversation history from recent messages
        recent = self.env["mail.message"].search([
            ("res_id", "=", self.id),
            ("model", "=", self._name),
            ("message_type", "in", ("comment", "email")),
        ], order="date asc", limit=agent.ai_max_turns)

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

        # Tell the agent WHO it is assisting (active user) so it can address them
        # by name and act on their behalf / within what they are permitted to see.
        requester_prefix = self._daisy_requester_context(message)
        if requester_prefix:
            context_prefix = requester_prefix + context_prefix

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
