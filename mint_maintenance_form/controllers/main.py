import base64
import logging
from datetime import date

from odoo import http
from odoo.http import request

MAX_UPLOAD_FILES = 5
MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB per file
ALLOWED_MIMETYPES = {
    "image/jpeg", "image/png", "image/gif", "image/webp",
    "image/heic", "image/heif", "image/bmp", "image/tiff",
    "application/pdf",
}

_logger = logging.getLogger(__name__)

PRIORITY_OPTIONS = [
    ("0", "Very Low"),
    ("1", "Low"),
    ("2", "Normal"),
    ("3", "High"),
]

MAINTENANCE_TYPES = [
    ("corrective", "Corrective"),
    ("preventive", "Preventive"),
]


class MaintenanceFormController(http.Controller):

    def _form_context(self, **extra):
        teams = request.env["maintenance.team"].sudo().search([], order="name")
        categories = (
            request.env["maintenance.equipment.category"]
            .sudo()
            .search([], order="name")
        )
        companies = (
            request.env["res.company"]
            .sudo()
            .search([("parent_id", "!=", False)], order="name")
        )
        ctx = {
            "teams": teams,
            "categories": categories,
            "companies": companies,
            "priorities": PRIORITY_OPTIONS,
            "maintenance_types": MAINTENANCE_TYPES,
            "error": None,
            "success": None,
            "form_values": {},
        }
        ctx.update(extra)
        return ctx

    @http.route(
        "/maintenance/request",
        type="http",
        auth="public",
        website=True,
        methods=["GET", "POST"],
    )
    def maintenance_form(self, **post):
        if request.httprequest.method == "GET":
            return request.render(
                "mint_maintenance_form.request_form", self._form_context()
            )

        # --- POST: validate & create ---
        ctx = self._form_context(form_values=post)

        name = post.get("name", "").strip()
        team_id = post.get("maintenance_team_id")
        description = post.get("description", "").strip()
        priority = post.get("priority", "2")
        submitter_name = post.get("submitter_name", "").strip()
        submitter_email = post.get("submitter_email", "").strip()

        if not name:
            ctx["error"] = "Request title is required."
            return request.render("mint_maintenance_form.request_form", ctx)
        if not team_id:
            ctx["error"] = "Please select a maintenance team."
            return request.render("mint_maintenance_form.request_form", ctx)
        if not description:
            ctx["error"] = "Description is required."
            return request.render("mint_maintenance_form.request_form", ctx)

        vals = {
            "name": name,
            "maintenance_team_id": int(team_id),
            "description": description,
            "priority": priority,
            "request_date": date.today(),
        }

        # Track submitter info in the description if provided
        if submitter_name or submitter_email:
            submitter_info = []
            if submitter_name:
                submitter_info.append(f"Submitted by: {submitter_name}")
            if submitter_email:
                submitter_info.append(f"Email: {submitter_email}")
            vals["description"] = "\n".join(submitter_info) + "\n\n" + description

        # Set owner to current user if logged in
        if request.env.uid and request.env.uid != request.env.ref("base.public_user").id:
            vals["owner_user_id"] = request.env.uid

        maintenance_type = post.get("maintenance_type")
        if maintenance_type:
            vals["maintenance_type"] = maintenance_type

        company_id = post.get("company_id")
        if company_id:
            vals["company_id"] = int(company_id)

        category_id = post.get("category_id")
        if category_id:
            vals["category_id"] = int(category_id)

        # Collect uploaded files
        uploaded_files = request.httprequest.files.getlist("photos")

        # Validate uploads before creating the record
        valid_files = []
        for f in uploaded_files:
            if not f or not f.filename:
                continue
            if len(valid_files) >= MAX_UPLOAD_FILES:
                ctx["error"] = f"Maximum {MAX_UPLOAD_FILES} files allowed."
                return request.render("mint_maintenance_form.request_form", ctx)
            if f.mimetype not in ALLOWED_MIMETYPES:
                ctx["error"] = (
                    f"File '{f.filename}' has an unsupported type. "
                    "Please upload images or PDFs only."
                )
                return request.render("mint_maintenance_form.request_form", ctx)
            data = f.read()
            if len(data) > MAX_UPLOAD_BYTES:
                ctx["error"] = (
                    f"File '{f.filename}' exceeds the 10 MB size limit."
                )
                return request.render("mint_maintenance_form.request_form", ctx)
            valid_files.append((f.filename, f.mimetype, data))

        try:
            maint_req = request.env["maintenance.request"].sudo().create(vals)

            # Attach uploaded photos to the maintenance request
            Attachment = request.env["ir.attachment"].sudo()
            for filename, mimetype, data in valid_files:
                Attachment.create({
                    "name": filename,
                    "datas": base64.b64encode(data),
                    "res_model": "maintenance.request",
                    "res_id": maint_req.id,
                    "mimetype": mimetype,
                })

            ctx["success"] = (
                "Your maintenance request has been submitted successfully!"
            )
            ctx["form_values"] = {}
        except Exception as e:
            _logger.exception("Failed to create maintenance request: %s", e)
            ctx["error"] = (
                "An error occurred while submitting your request. Please try again."
            )

        return request.render("mint_maintenance_form.request_form", ctx)
