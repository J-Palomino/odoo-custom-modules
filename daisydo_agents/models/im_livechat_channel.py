import logging
from odoo import models, fields

_logger = logging.getLogger(__name__)


class ImLivechatChannel(models.Model):
    _inherit = "im_livechat.channel"

    ai_first = fields.Boolean(
        string="AI-First Routing",
        default=True,
        help="Route new conversations to AI agents before human operators",
    )

    def _get_operator(self, previous_operator_id=None, lang=None, country_id=None, expertises=None, users=None):
        """Override to prefer AI agents when ai_first is enabled."""
        self.ensure_one()
        if not self.ai_first:
            return super()._get_operator(
                previous_operator_id=previous_operator_id, lang=lang,
                country_id=country_id, expertises=expertises, users=users,
            )

        # Find active agents assigned to this channel
        agents = self.env["daisy.agent"].sudo().search([
            ("livechat_channel_ids", "in", self.id),
            ("state", "=", "active"),
        ])
        if not agents:
            return super()._get_operator(
                previous_operator_id=previous_operator_id, lang=lang,
                country_id=country_id, expertises=expertises, users=users,
            )

        # Pick agent with fewest active conversations
        best = min(agents, key=lambda a: a.active_conversations)
        if best.active_conversations < best.max_concurrent_chats and best.user_id:
            return best.user_id

        # All agents at capacity — fall back to human
        return super()._get_operator(
            previous_operator_id=previous_operator_id, lang=lang,
            country_id=country_id, expertises=expertises, users=users,
        )
