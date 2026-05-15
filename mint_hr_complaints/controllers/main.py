import base64
import logging
from datetime import datetime

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)

MAX_UPLOAD_FILES = 5
MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB per file
ALLOWED_MIMETYPES = {
    "image/jpeg", "image/png", "image/gif", "image/webp",
    "image/heic", "image/heif", "image/bmp", "image/tiff",
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "text/plain",
}

CONCERN_TYPES = [
    ("harassment", "Harassment"),
    ("discrimination", "Discrimination"),
    ("retaliation", "Retaliation"),
    ("conduct", "Conduct"),
    ("safety", "Safety"),
    ("policy_violation", "Policy Violation"),
    ("other", "Other"),
]
URGENCY_OPTIONS = [
    ("low", "Low"),
    ("medium", "Medium"),
    ("high", "High"),
    ("critical", "Critical"),
]
CONCERN_TYPE_KEYS = {k for k, _ in CONCERN_TYPES}
URGENCY_KEYS = {k for k, _ in URGENCY_OPTIONS}


class HrComplaintController(http.Controller):

    def _ctx(self, form_values=None, error=None, success=None):
        return {
            "concern_types": CONCERN_TYPES,
            "urgency_options": URGENCY_OPTIONS,
            "form_values": form_values or {},
            "error": error,
            "success": success,
        }

    @http.route(
        ["/report-concern"],
        type="http",
        auth="public",
        website=True,
        csrf=True,
        methods=["GET"],
    )
    def report_concern_form(self, **kwargs):
        return request.render(
            "mint_hr_complaints.report_concern_form",
            self._ctx(),
        )

    @http.route(
        ["/report-concern"],
        type="http",
        auth="public",
        website=True,
        csrf=True,
        methods=["POST"],
    )
    def report_concern_submit(self, **post):
        form_values = {k: (v or "").strip() for k, v in post.items() if isinstance(v, str)}
        form_values["is_anonymous"] = bool(post.get("is_anonymous"))
        form_values["is_safety_concern"] = bool(post.get("is_safety_concern"))
        form_values["is_ongoing"] = bool(post.get("is_ongoing"))

        error = self._validate(form_values)
        if error:
            return request.render(
                "mint_hr_complaints.report_concern_form",
                self._ctx(form_values=form_values, error=error),
            )

        valid_files, upload_error = self._validate_uploads()
        if upload_error:
            return request.render(
                "mint_hr_complaints.report_concern_form",
                self._ctx(form_values=form_values, error=upload_error),
            )

        vals = self._build_vals(form_values)

        try:
            complaint = request.env["mint.hr.complaint"].sudo().create(vals)
        except Exception:
            _logger.exception("Failed to create HR complaint")
            return request.render(
                "mint_hr_complaints.report_concern_form",
                self._ctx(
                    form_values=form_values,
                    error="An error occurred while submitting your report. Please try again.",
                ),
            )

        self._save_attachments(complaint, valid_files)

        return request.render(
            "mint_hr_complaints.report_concern_form",
            self._ctx(success=complaint.name),
        )

    def _validate(self, values):
        if not values.get("subject_name"):
            return "Subject Name is required."
        if values.get("concern_type") not in CONCERN_TYPE_KEYS:
            return "Please select a valid Concern Type."
        if not values.get("incident_date"):
            return "Incident Date is required."
        try:
            datetime.strptime(values["incident_date"], "%Y-%m-%d")
        except ValueError:
            return "Incident Date must be a valid date (YYYY-MM-DD)."
        if not values.get("description"):
            return "Detailed Description is required."
        urgency = values.get("urgency") or "medium"
        if urgency not in URGENCY_KEYS:
            return "Please select a valid Urgency Level."
        return None

    def _validate_uploads(self):
        files = request.httprequest.files.getlist("attachments")
        valid = []
        for f in files:
            if not f or not f.filename:
                continue
            if len(valid) >= MAX_UPLOAD_FILES:
                return None, f"Maximum {MAX_UPLOAD_FILES} files allowed."
            if f.mimetype not in ALLOWED_MIMETYPES:
                return None, (
                    f"File '{f.filename}' has an unsupported type. "
                    "Please upload images, PDFs, Word, or text files."
                )
            data = f.read()
            if len(data) > MAX_UPLOAD_BYTES:
                return None, f"File '{f.filename}' exceeds the 10 MB size limit."
            valid.append((f.filename, f.mimetype, data))
        return valid, None

    def _build_vals(self, values):
        is_anonymous = values.get("is_anonymous")
        vals = {
            "is_anonymous": is_anonymous,
            "subject_name": values["subject_name"],
            "subject_position": values.get("subject_position") or False,
            "concern_type": values["concern_type"],
            "incident_date": values["incident_date"],
            "incident_location": values.get("incident_location") or False,
            "description": values["description"],
            "witnesses": values.get("witnesses") or False,
            "evidence_description": values.get("evidence_description") or False,
            "urgency": values.get("urgency") or "medium",
            "is_safety_concern": values["is_safety_concern"],
            "is_ongoing": values["is_ongoing"],
        }
        if not is_anonymous:
            vals["complainant_name"] = values.get("complainant_name") or False
            vals["complainant_position"] = values.get("complainant_position") or False
        return vals

    def _save_attachments(self, complaint, valid_files):
        if not valid_files:
            return
        Attachment = request.env["ir.attachment"].sudo()
        for filename, mimetype, data in valid_files:
            Attachment.create({
                "name": filename,
                "datas": base64.b64encode(data),
                "res_model": complaint._name,
                "res_id": complaint.id,
                "mimetype": mimetype,
            })
