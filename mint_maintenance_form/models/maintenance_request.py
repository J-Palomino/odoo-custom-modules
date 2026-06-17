from odoo import fields, models


class MaintenanceRequest(models.Model):
    _inherit = "maintenance.request"

    x_intake_channel = fields.Selection(
        selection=[
            ("phone", "Phone"),
            ("sms", "SMS"),
            ("web", "Web Form"),
            ("manual", "Manual"),
        ],
        string="Intake Channel",
        index=True,
        help="How this ticket came in. Set automatically by the Phone-Operator "
        "(phone) and SMS-Manager (sms) agents on create, and by the website "
        "request forms (web). Left blank for tickets opened directly in Odoo "
        "unless explicitly set to 'manual'.",
    )
