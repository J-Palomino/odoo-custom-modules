from odoo import fields, models


class ProjectProject(models.Model):
    _inherit = "project.project"

    daisy_agent_id = fields.Many2one(
        "daisy.agent",
        string="Daisy Agent Inbox",
        index=True,
        ondelete="set null",
        help="Set when this project is a Daisy agent's private email inbox. "
        "Such projects (and their tasks) are visible only to the agent's "
        "manager, the agent itself, and Daisy administrators.",
    )
