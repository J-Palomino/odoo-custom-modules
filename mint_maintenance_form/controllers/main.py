import base64
import json
import logging
from datetime import date
from html import escape as html_escape

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)

MAX_UPLOAD_FILES = 5
MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB per file
ALLOWED_MIMETYPES = {
    "image/jpeg", "image/png", "image/gif", "image/webp",
    "image/heic", "image/heif", "image/bmp", "image/tiff",
    "application/pdf",
}

IT_TEAM_ID = 2
NEW_REQUEST_STAGE = 1

EQUIPMENT_TYPES = [
    ("computer", "Computer"),
    ("printer", "Printer"),
    ("scanner", "Scanner"),
    ("pos", "POS Equipment"),
    ("security_camera", "Security Camera"),
    ("software", "Software / Email"),
    ("ipad", "iPad"),
    ("telephone", "Telephone"),
    ("drive_thru", "Drive Thru"),
    ("tv", "TV"),
]

SUBTYPE_OPTIONS = {
    "computer": [
        ("laptop_mac", "Laptop Mac"),
        ("computer_windows", "PC / Windows"),
    ],
    "printer": [
        ("receipt", "Receipt Printer"),
        ("zebra", "Zebra Label Printer"),
        ("printnode", "Printnode"),
        ("other_printer", "Other Printer"),
    ],
    "scanner": [
        ("passport", "Passport / License Scanner"),
        ("paper", "Paper Scanner"),
    ],
    "pos": [
        ("pos_monitor", "Monitor"),
        ("pos_computer", "Computer"),
        ("pos_keyboard", "Keyboard"),
        ("pos_mouse", "Mouse"),
        ("dutchie_pay", "Dutchie Pay Terminal"),
        ("deli_scale", "Deli Scale"),
    ],
    "tv": [
        ("apple_tv", "Apple TV"),
        ("tv_other", "TV (Other)"),
    ],
}

# Maps (equipment_type, subtype) -> maintenance.equipment ID in Odoo
EQUIPMENT_ID_MAP = {
    ("computer", "laptop_mac"): 12,
    ("computer", "computer_windows"): 13,
    ("printer", "receipt"): 14,
    ("printer", "zebra"): 15,
    ("printer", "other_printer"): 16,
    ("printer", "printnode"): 17,
    ("scanner", "passport"): 18,
    ("scanner", "paper"): 19,
    ("pos", "pos_monitor"): 20,
    ("pos", "pos_computer"): 21,
    ("pos", "pos_keyboard"): 22,
    ("pos", "pos_mouse"): 23,
    ("pos", "dutchie_pay"): 24,
    ("pos", "deli_scale"): 25,
    ("security_camera", ""): 26,
    ("software", ""): 27,
    ("ipad", ""): 28,
    ("telephone", ""): 29,
    ("drive_thru", ""): 30,
    ("tv", "apple_tv"): 31,
    ("tv", "tv_other"): 32,
}

PRIORITY_OPTIONS = [
    ("0", "Very Low"),
    ("1", "Low"),
    ("2", "Normal"),
    ("3", "High"),
]


class MaintenanceFormController(http.Controller):

    def _form_context(self, **extra):
        companies = (
            request.env["res.company"]
            .sudo()
            .search([("parent_id", "!=", False)], order="name")
        )
        company_list = []
        states = set()
        for c in companies:
            state_name = c.state_id.name if c.state_id else ""
            company_list.append({
                "id": c.id,
                "name": c.name,
                "state": state_name,
            })
            if state_name:
                states.add(state_name)

        ctx = {
            "companies": company_list,
            "states": sorted(states),
            "equipment_types": EQUIPMENT_TYPES,
            "subtype_options_json": json.dumps(SUBTYPE_OPTIONS),
            "priorities": PRIORITY_OPTIONS,
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

        ctx = self._form_context(form_values=post)

        submitter_name = post.get("submitter_name", "").strip()
        submitter_email = post.get("submitter_email", "").strip()
        equipment_type = post.get("equipment_type", "").strip()
        subtype = post.get("subtype", "").strip()
        state = post.get("state", "").strip()
        description = post.get("description", "").strip()
        priority = post.get("priority", "2")

        if not submitter_name:
            ctx["error"] = "Your name is required."
            return request.render("mint_maintenance_form.request_form", ctx)
        if not submitter_email:
            ctx["error"] = "Your email is required."
            return request.render("mint_maintenance_form.request_form", ctx)
        if not equipment_type:
            ctx["error"] = "Please select an equipment type."
            return request.render("mint_maintenance_form.request_form", ctx)
        if not description:
            ctx["error"] = "Description is required."
            return request.render("mint_maintenance_form.request_form", ctx)

        # Resolve store name
        company_id = post.get("company_id")
        store_name = ""
        if company_id:
            company = request.env["res.company"].sudo().browse(int(company_id))
            if company.exists():
                store_name = company.name

        # Build equipment label
        equip_type_label = dict(EQUIPMENT_TYPES).get(equipment_type, "")
        subtype_label = ""
        for opts in SUBTYPE_OPTIONS.values():
            for val, label in opts:
                if val == subtype:
                    subtype_label = label
                    break
        equipment_label = (
            f"{equip_type_label} - {subtype_label}" if subtype_label
            else equip_type_label
        )

        # Auto-generate request title
        title = store_name or "IT Request"
        if equipment_label:
            title += " - " + equipment_label
        desc_preview = description[:60].split("\n")[0]
        if desc_preview:
            title += " - " + desc_preview
        title = title[:128]

        # Build rich HTML description
        desc_parts = [
            f"<p><strong>Submitted by:</strong> {html_escape(submitter_name)}</p>",
            f"<p><strong>Email:</strong> {html_escape(submitter_email)}</p>",
        ]
        if state:
            desc_parts.append(
                f"<p><strong>State:</strong> {html_escape(state)}</p>"
            )
        if store_name:
            desc_parts.append(
                f"<p><strong>Store:</strong> {html_escape(store_name)}</p>"
            )
        if equipment_label:
            desc_parts.append(
                f"<p><strong>Equipment:</strong> {html_escape(equipment_label)}</p>"
            )
        desc_parts.append(f"<br/>{html_escape(description)}")
        desc_html = "".join(desc_parts)

        vals = {
            "name": title,
            "description": desc_html,
            "maintenance_type": "corrective",
            "priority": priority,
            "maintenance_team_id": IT_TEAM_ID,
            "stage_id": NEW_REQUEST_STAGE,
            "request_date": date.today(),
            "email_cc": submitter_email,
        }

        # Map equipment selection to maintenance.equipment ID
        equip_key = (equipment_type, subtype) if subtype else (equipment_type, "")
        equipment_id = EQUIPMENT_ID_MAP.get(equip_key)
        if equipment_id:
            vals["equipment_id"] = equipment_id

        if company_id:
            vals["company_id"] = int(company_id)

        if request.env.uid and request.env.uid != request.env.ref("base.public_user").id:
            vals["owner_user_id"] = request.env.uid

        # Validate uploaded files
        uploaded_files = request.httprequest.files.getlist("photos")
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
                "An error occurred while submitting your request. "
                "Please try again."
            )

        return request.render("mint_maintenance_form.request_form", ctx)
