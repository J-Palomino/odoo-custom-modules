from odoo import api, fields, models


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

    x_time_to_close = fields.Integer(
        string="Days to Close",
        compute="_compute_x_time_to_close",
        store=True,
        aggregator="avg",
        help="Whole calendar days between the Request Date and the Close Date. "
        "Populated only once a ticket reaches a closing (done) stage; stays blank "
        "while the ticket is open. Use this as a pivot/graph measure (it averages "
        "by default) to track each team's average time to close a ticket.",
    )

    @api.depends("request_date", "close_date")
    def _compute_x_time_to_close(self):
        """Elapsed calendar days from request to close.

        ``request_date`` and ``close_date`` are both Date fields (day
        granularity), so this is whole days. Left empty while the ticket is
        still open; the reporting views filter on a set Close Date so open
        tickets never skew the average.
        """
        for rec in self:
            if rec.request_date and rec.close_date:
                rec.x_time_to_close = (rec.close_date - rec.request_date).days
            else:
                rec.x_time_to_close = False
