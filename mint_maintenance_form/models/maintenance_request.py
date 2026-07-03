from odoo import fields, models


class MaintenanceRequest(models.Model):
    _inherit = "maintenance.request"

    # Restrict maintenance assignees to INTERNAL users. Portal accounts
    # (web/customer logins, share=True) must never be offered as a Technician.
    # This field-level domain applies to every view (list/kanban/search) and the
    # activity picker; the form view further narrows it to the selected team's
    # members. Without this, base maintenance offered all res.users — including
    # the ~397 portal accounts — as assignable.
    user_id = fields.Many2one(domain="[('share', '=', False)]")

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
