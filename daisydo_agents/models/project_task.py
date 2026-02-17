from odoo import api, models
from odoo.tools import email_normalize


class ProjectTask(models.Model):
    _inherit = "project.task"

    @api.model
    def message_new(self, msg_dict, custom_values=None):
        """Auto-assign agent when an email creates a task in their project."""
        task = super().message_new(msg_dict, custom_values=custom_values)
        if task.project_id:
            agent = self.env["daisy.agent"].sudo().search([
                ("mail_project_id", "=", task.project_id.id),
                ("state", "=", "active"),
            ], limit=1)
            if agent and agent.user_id:
                task.user_ids = [(4, agent.user_id.id)]
                task.message_subscribe(partner_ids=[agent.partner_id.id])
        # Subscribe the email sender so they receive the AI reply
        email_from = msg_dict.get("email_from") or msg_dict.get("from")
        if email_from:
            normalized = email_normalize(email_from)
            if normalized:
                partner = self.env["res.partner"].sudo().search(
                    [("email_normalized", "=", normalized)], limit=1
                )
                if partner:
                    task.message_subscribe(partner_ids=partner.ids)
        return task
