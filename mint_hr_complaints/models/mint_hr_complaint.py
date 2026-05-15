import logging
from datetime import timedelta

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)

RETENTION_DAYS = 365
HARD_DELETE_BUFFER_DAYS = 30
HR_CHANNEL_XMLID = "mint_hr_complaints.channel_hr_complaints"
HR_GROUP_XMLID = "hr.group_hr_user"


class MintHrComplaint(models.Model):
    _name = "mint.hr.complaint"
    _description = "HR Workplace Concern Complaint"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "create_date desc"

    name = fields.Char(
        string="Reference",
        required=True,
        copy=False,
        readonly=True,
        default=lambda self: _("New"),
    )
    state = fields.Selection(
        [
            ("new", "New"),
            ("reviewing", "Reviewing"),
            ("investigating", "Investigating"),
            ("resolved", "Resolved"),
            ("closed", "Closed"),
        ],
        string="Status",
        default="new",
        required=True,
        tracking=True,
    )
    active = fields.Boolean(default=True)

    is_anonymous = fields.Boolean(string="Anonymous", tracking=True)
    complainant_name = fields.Char(string="Complainant Name", tracking=True)
    complainant_position = fields.Char(string="Complainant Position / Dept")
    subject_name = fields.Char(string="Subject Name", required=True, tracking=True)
    subject_position = fields.Char(string="Subject Position / Dept")

    concern_type = fields.Selection(
        [
            ("harassment", "Harassment"),
            ("discrimination", "Discrimination"),
            ("retaliation", "Retaliation"),
            ("conduct", "Conduct"),
            ("safety", "Safety"),
            ("policy_violation", "Policy Violation"),
            ("other", "Other"),
        ],
        string="Concern Type",
        required=True,
        tracking=True,
    )
    incident_date = fields.Date(string="Incident Date", required=True)
    incident_location = fields.Char(string="Incident Location")
    description = fields.Text(string="Detailed Description", required=True)
    witnesses = fields.Text(string="Witnesses")
    evidence_description = fields.Text(string="Evidence Provided")

    urgency = fields.Selection(
        [
            ("low", "Low"),
            ("medium", "Medium"),
            ("high", "High"),
            ("critical", "Critical"),
        ],
        string="Urgency",
        default="medium",
        tracking=True,
    )
    is_safety_concern = fields.Boolean(string="Safety Concern", tracking=True)
    is_ongoing = fields.Boolean(string="Ongoing Issue", tracking=True)

    submitted_at = fields.Datetime(
        string="Submitted At",
        default=fields.Datetime.now,
        readonly=True,
    )
    retention_expiry = fields.Date(
        string="Retention Expiry",
        compute="_compute_retention_expiry",
        store=True,
        help="Date on which this complaint becomes eligible for archiving.",
    )
    attachment_count = fields.Integer(
        string="Attachments",
        compute="_compute_attachment_count",
    )

    @api.depends("create_date")
    def _compute_retention_expiry(self):
        for rec in self:
            if rec.create_date:
                rec.retention_expiry = rec.create_date.date() + timedelta(days=RETENTION_DAYS)
            else:
                rec.retention_expiry = False

    def _compute_attachment_count(self):
        Attachment = self.env["ir.attachment"].sudo()
        for rec in self:
            rec.attachment_count = Attachment.search_count([
                ("res_model", "=", self._name),
                ("res_id", "=", rec.id),
            ])

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get("name") or vals.get("name") == _("New"):
                vals["name"] = self.env["ir.sequence"].next_by_code(
                    "mint.hr.complaint"
                ) or _("New")
            if vals.get("is_anonymous"):
                vals["complainant_name"] = False
                vals["complainant_position"] = False
        records = super().create(vals_list)
        for rec in records:
            rec._notify_hr_team()
        return records

    def _get_hr_user_partners(self):
        hr_group = self.env.ref(HR_GROUP_XMLID, raise_if_not_found=False)
        if not hr_group:
            return self.env["res.partner"]
        return hr_group.sudo().users.mapped("partner_id")

    def _notify_hr_team(self):
        self.ensure_one()
        partners = self._get_hr_user_partners()
        if partners:
            self.sudo().message_subscribe(partner_ids=partners.ids)
        summary = self._format_notification_body()
        self.sudo().message_post(
            body=summary,
            subject=_("New workplace concern: %s") % self.name,
            message_type="notification",
            subtype_xmlid="mail.mt_comment",
            partner_ids=partners.ids,
        )
        self._post_to_hr_channel(summary)

    def _format_notification_body(self):
        self.ensure_one()
        concern_label = dict(self._fields["concern_type"].selection).get(
            self.concern_type, self.concern_type or ""
        )
        urgency_label = dict(self._fields["urgency"].selection).get(
            self.urgency, self.urgency or ""
        )
        anon = _("Anonymous") if self.is_anonymous else (self.complainant_name or _("Not provided"))
        lines = [
            _("<b>Reference:</b> %s") % self.name,
            _("<b>Concern Type:</b> %s") % concern_label,
            _("<b>Urgency:</b> %s") % urgency_label,
            _("<b>Complainant:</b> %s") % anon,
            _("<b>Subject:</b> %s") % (self.subject_name or ""),
            _("<b>Incident Date:</b> %s") % (self.incident_date or ""),
            _("<b>Safety Concern:</b> %s") % (_("Yes") if self.is_safety_concern else _("No")),
            _("<b>Ongoing:</b> %s") % (_("Yes") if self.is_ongoing else _("No")),
        ]
        return "<br/>".join(str(line) for line in lines)

    def _post_to_hr_channel(self, body):
        self.ensure_one()
        channel = self.env.ref(HR_CHANNEL_XMLID, raise_if_not_found=False)
        if not channel:
            _logger.warning("HR Complaints channel missing (%s)", HR_CHANNEL_XMLID)
            return
        try:
            url = "/odoo/action-mint_hr_complaints.action_mint_hr_complaint/%s" % self.id
            link = _('<a href="%s">%s</a>') % (url, self.name)
            channel.sudo().message_post(
                body=_("New complaint: %s<br/>%s") % (link, body),
                message_type="notification",
                subtype_xmlid="mail.mt_comment",
            )
        except Exception:
            _logger.exception("Failed to post to HR complaints channel for %s", self.name)

    def action_set_reviewing(self):
        self.write({"state": "reviewing"})

    def action_set_investigating(self):
        self.write({"state": "investigating"})

    def action_set_resolved(self):
        self.write({"state": "resolved"})

    def action_set_closed(self):
        self.write({"state": "closed"})

    def action_view_attachments(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Attachments"),
            "res_model": "ir.attachment",
            "view_mode": "list,form",
            "domain": [("res_model", "=", self._name), ("res_id", "=", self.id)],
            "context": {"default_res_model": self._name, "default_res_id": self.id},
        }

    @api.model
    def _cron_archive_expired(self):
        cutoff = fields.Datetime.now() - timedelta(days=RETENTION_DAYS)
        expired = self.search([("create_date", "<", cutoff), ("active", "=", True)])
        if expired:
            _logger.info("Archiving %s expired HR complaint(s)", len(expired))
            expired.write({"active": False})

    @api.model
    def _cron_hard_delete_archived(self):
        cutoff = fields.Datetime.now() - timedelta(
            days=RETENTION_DAYS + HARD_DELETE_BUFFER_DAYS
        )
        obsolete = self.with_context(active_test=False).search([
            ("create_date", "<", cutoff),
            ("active", "=", False),
        ])
        if obsolete:
            _logger.info("Hard-deleting %s archived HR complaint(s)", len(obsolete))
            Attachment = self.env["ir.attachment"].sudo()
            Attachment.search([
                ("res_model", "=", self._name),
                ("res_id", "in", obsolete.ids),
            ]).unlink()
            obsolete.unlink()
