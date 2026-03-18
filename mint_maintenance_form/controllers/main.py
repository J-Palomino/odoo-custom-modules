import base64
import json
import logging
from datetime import date
from html import escape as html_escape

from markupsafe import Markup
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

FACILITIES_EQUIPMENT_TYPES = [
    ("hvac", "HVAC"),
    ("plumbing", "Plumbing"),
    ("electrical", "Electrical"),
    ("janitorial", "Janitorial / Cleaning"),
    ("building_access", "Building Access / Locks"),
    ("parking", "Parking Lot"),
    ("signage", "Signage"),
    ("furniture", "Furniture / Fixtures"),
]

FACILITIES_EQUIPMENT_XMLID_MAP = {
    "hvac": "mint_maintenance_form.equip_facilities_hvac",
    "plumbing": "mint_maintenance_form.equip_facilities_plumbing",
    "electrical": "mint_maintenance_form.equip_facilities_electrical",
    "janitorial": "mint_maintenance_form.equip_facilities_janitorial",
    "building_access": "mint_maintenance_form.equip_facilities_building_access",
    "parking": "mint_maintenance_form.equip_facilities_parking",
    "signage": "mint_maintenance_form.equip_facilities_signage",
    "furniture": "mint_maintenance_form.equip_facilities_furniture",
}

FACILITIES_TEAM_XMLID = "mint_maintenance_form.team_facilities"

PRIORITY_OPTIONS = [
    ("0", "Very Low"),
    ("1", "Low"),
    ("2", "Normal"),
    ("3", "High"),
]


class MaintenanceFormController(http.Controller):

    def _check_request_access(self, maint_req, user):
        """Return True if user owns this request."""
        if not maint_req.exists():
            return False
        if maint_req.owner_user_id and maint_req.owner_user_id.id == user.id:
            return True
        if user.email and user.email.lower() in (maint_req.email_cc or "").lower():
            return True
        return False

    def _form_context(self, equipment_types=None, subtype_options=None, **extra):
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
            "equipment_types": equipment_types or EQUIPMENT_TYPES,
            "subtype_options_json": Markup(json.dumps(subtype_options or SUBTYPE_OPTIONS)),
            "priorities": PRIORITY_OPTIONS,
            "error": None,
            "success": None,
            "form_values": {},
            "is_logged_in": False,
        }
        ctx.update(extra)
        return ctx

    def _get_user_prefill(self):
        """Return (prefill_dict, is_logged_in) for the current user."""
        prefill = {}
        logged_in = False
        user = request.env.user
        if user and user.id != request.env.ref("base.public_user").id:
            logged_in = True
            prefill["submitter_name"] = user.name or ""
            prefill["submitter_email"] = user.email or ""
            if user.company_id and user.company_id.parent_id:
                prefill["company_id"] = str(user.company_id.id)
                state_name = user.company_id.state_id.name if user.company_id.state_id else ""
                if state_name:
                    prefill["state"] = state_name
        return prefill, logged_in

    def _validate_common_fields(self, post):
        """Validate shared fields. Returns error string or None."""
        if not post.get("submitter_name", "").strip():
            return "Your name is required."
        if not post.get("submitter_email", "").strip():
            return "Your email is required."
        if not post.get("description", "").strip():
            return "Description is required."
        return None

    def _validate_uploads(self, template, ctx):
        """Validate uploaded files. Returns (valid_files, error_response) tuple."""
        uploaded_files = request.httprequest.files.getlist("photos")
        valid_files = []
        for f in uploaded_files:
            if not f or not f.filename:
                continue
            if len(valid_files) >= MAX_UPLOAD_FILES:
                ctx["error"] = f"Maximum {MAX_UPLOAD_FILES} files allowed."
                return None, request.render(template, ctx)
            if f.mimetype not in ALLOWED_MIMETYPES:
                ctx["error"] = (
                    f"File '{f.filename}' has an unsupported type. "
                    "Please upload images or PDFs only."
                )
                return None, request.render(template, ctx)
            data = f.read()
            if len(data) > MAX_UPLOAD_BYTES:
                ctx["error"] = (
                    f"File '{f.filename}' exceeds the 10 MB size limit."
                )
                return None, request.render(template, ctx)
            valid_files.append((f.filename, f.mimetype, data))
        return valid_files, None

    def _create_request(self, vals, valid_files, template, ctx, success_msg):
        """Create maintenance.request + attachments. Returns rendered response."""
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

            ctx["success"] = success_msg
            ctx["form_values"] = {}
        except Exception as e:
            _logger.exception("Failed to create maintenance request: %s", e)
            ctx["error"] = (
                "An error occurred while submitting your request. "
                "Please try again."
            )

        return request.render(template, ctx)

    # ------------------------------------------------------------------
    # /fixit — routing page
    # ------------------------------------------------------------------
    @http.route(
        "/fixit",
        type="http",
        auth="public",
        website=True,
        methods=["GET"],
    )
    def fixit_routing(self, **kw):
        return request.render("mint_maintenance_form.routing_page")

    # ------------------------------------------------------------------
    # /it-requests — IT equipment form
    # ------------------------------------------------------------------
    @http.route(
        "/it-requests",
        type="http",
        auth="public",
        website=True,
        methods=["GET", "POST"],
    )
    def it_request_form(self, **post):
        template = "mint_maintenance_form.it_request_form"

        if request.httprequest.method == "GET":
            prefill, logged_in = self._get_user_prefill()
            return request.render(
                template,
                self._form_context(form_values=prefill, is_logged_in=logged_in),
            )

        ctx = self._form_context(form_values=post)

        submitter_name = post.get("submitter_name", "").strip()
        submitter_email = post.get("submitter_email", "").strip()
        equipment_type = post.get("equipment_type", "").strip()
        subtype = post.get("subtype", "").strip()
        state = post.get("state", "").strip()
        description = post.get("description", "").strip()
        priority = post.get("priority", "2")

        # Validation
        error = self._validate_common_fields(post)
        if error:
            ctx["error"] = error
            return request.render(template, ctx)
        if not equipment_type:
            ctx["error"] = "Please select an equipment type."
            return request.render(template, ctx)

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
        valid_files, error_resp = self._validate_uploads(template, ctx)
        if error_resp:
            return error_resp

        return self._create_request(
            vals, valid_files, template, ctx,
            "Your IT request has been submitted successfully!",
        )

    # ------------------------------------------------------------------
    # /maintenance-requests — Facilities maintenance form
    # ------------------------------------------------------------------
    @http.route(
        "/maintenance-requests",
        type="http",
        auth="public",
        website=True,
        methods=["GET", "POST"],
    )
    def facilities_request_form(self, **post):
        template = "mint_maintenance_form.facilities_request_form"

        if request.httprequest.method == "GET":
            prefill, logged_in = self._get_user_prefill()
            return request.render(
                template,
                self._form_context(
                    equipment_types=FACILITIES_EQUIPMENT_TYPES,
                    subtype_options={},
                    form_values=prefill,
                    is_logged_in=logged_in,
                ),
            )

        ctx = self._form_context(
            equipment_types=FACILITIES_EQUIPMENT_TYPES,
            subtype_options={},
            form_values=post,
        )

        submitter_name = post.get("submitter_name", "").strip()
        submitter_email = post.get("submitter_email", "").strip()
        category = post.get("equipment_type", "").strip()
        state = post.get("state", "").strip()
        description = post.get("description", "").strip()
        priority = post.get("priority", "2")

        # Validation
        error = self._validate_common_fields(post)
        if error:
            ctx["error"] = error
            return request.render(template, ctx)
        if not category:
            ctx["error"] = "Please select a category."
            return request.render(template, ctx)

        # Resolve store name
        company_id = post.get("company_id")
        store_name = ""
        if company_id:
            company = request.env["res.company"].sudo().browse(int(company_id))
            if company.exists():
                store_name = company.name

        # Category label
        category_label = dict(FACILITIES_EQUIPMENT_TYPES).get(category, "")

        # Auto-generate request title
        title = store_name or "Maintenance Request"
        if category_label:
            title += " - " + category_label
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
        if category_label:
            desc_parts.append(
                f"<p><strong>Category:</strong> {html_escape(category_label)}</p>"
            )
        desc_parts.append(f"<br/>{html_escape(description)}")
        desc_html = "".join(desc_parts)

        # Resolve team and equipment IDs via XML IDs
        try:
            facilities_team_id = request.env.ref(FACILITIES_TEAM_XMLID).id
        except Exception:
            _logger.warning("Facilities team XML ID not found, falling back to IT team")
            facilities_team_id = IT_TEAM_ID

        vals = {
            "name": title,
            "description": desc_html,
            "maintenance_type": "corrective",
            "priority": priority,
            "maintenance_team_id": facilities_team_id,
            "stage_id": NEW_REQUEST_STAGE,
            "request_date": date.today(),
            "email_cc": submitter_email,
        }

        # Resolve equipment ID from XML ID
        xmlid = FACILITIES_EQUIPMENT_XMLID_MAP.get(category)
        if xmlid:
            try:
                vals["equipment_id"] = request.env.ref(xmlid).id
            except Exception:
                _logger.warning("Equipment XML ID %s not found", xmlid)

        if company_id:
            vals["company_id"] = int(company_id)

        if request.env.uid and request.env.uid != request.env.ref("base.public_user").id:
            vals["owner_user_id"] = request.env.uid

        # Validate uploaded files
        valid_files, error_resp = self._validate_uploads(template, ctx)
        if error_resp:
            return error_resp

        return self._create_request(
            vals, valid_files, template, ctx,
            "Your maintenance request has been submitted successfully!",
        )

    # ------------------------------------------------------------------
    # /tickets — unified list
    # ------------------------------------------------------------------
    @http.route(
        "/tickets",
        type="http",
        auth="user",
        website=True,
        methods=["GET"],
    )
    def maintenance_requests(self, **kw):
        priority_labels = dict(PRIORITY_OPTIONS)
        MaintRequest = request.env["maintenance.request"].sudo()

        user = request.env.user
        requests_list = MaintRequest.search(
            [
                ("stage_id.done", "!=", True),
                "|",
                ("owner_user_id", "=", user.id),
                ("email_cc", "ilike", user.email or ""),
            ],
            order="request_date desc, id desc",
            limit=50,
        )

        items = []
        for r in requests_list:
            items.append({
                "id": r.id,
                "name": r.name or "",
                "stage": r.stage_id.name if r.stage_id else "New",
                "priority": priority_labels.get(r.priority, "Normal"),
                "request_date": r.request_date,
                "company": r.company_id.name if r.company_id else "",
                "equipment": r.equipment_id.name if r.equipment_id else "",
                "request_type": "IT" if r.maintenance_team_id.id == IT_TEAM_ID else "Facilities",
            })

        return request.render(
            "mint_maintenance_form.request_list",
            {"requests": items, "user": user},
        )

    # ------------------------------------------------------------------
    # /tickets/<id> — detail view
    # ------------------------------------------------------------------
    @http.route(
        "/tickets/<int:request_id>",
        type="http",
        auth="user",
        website=True,
        methods=["GET"],
    )
    def maintenance_request_detail(self, request_id, **kw):
        user = request.env.user
        maint_req = (
            request.env["maintenance.request"].sudo().browse(request_id)
        )
        if not self._check_request_access(maint_req, user):
            return request.redirect("/tickets")

        priority_labels = dict(PRIORITY_OPTIONS)

        request_type = "IT" if maint_req.maintenance_team_id.id == IT_TEAM_ID else "Facilities"

        # Get visible messages (exclude internal notes)
        messages = (
            request.env["mail.message"]
            .sudo()
            .search(
                [
                    ("model", "=", "maintenance.request"),
                    ("res_id", "=", request_id),
                    ("message_type", "in", ["comment", "email"]),
                    ("subtype_id.internal", "=", False),
                ],
                order="date asc",
            )
        )

        # Get attachments on the request itself (not on messages)
        attachments = (
            request.env["ir.attachment"]
            .sudo()
            .search(
                [
                    ("res_model", "=", "maintenance.request"),
                    ("res_id", "=", request_id),
                    ("res_field", "=", False),
                ],
            )
        )

        return request.render(
            "mint_maintenance_form.request_detail",
            {
                "req": maint_req,
                "messages": messages,
                "attachments": attachments,
                "priority_labels": priority_labels,
                "request_type": request_type,
            },
        )

    # ------------------------------------------------------------------
    # /tickets/<id>/reply — unchanged
    # ------------------------------------------------------------------
    @http.route(
        "/tickets/<int:request_id>/reply",
        type="http",
        auth="user",
        website=True,
        methods=["POST"],
    )
    def maintenance_request_reply(self, request_id, **post):
        user = request.env.user
        maint_req = (
            request.env["maintenance.request"].sudo().browse(request_id)
        )
        if not self._check_request_access(maint_req, user):
            return request.redirect("/tickets")

        body = post.get("message", "").strip()
        if not body:
            return request.redirect(f"/tickets/{request_id}")

        # Create attachments first
        Attachment = request.env["ir.attachment"].sudo()
        attachment_ids = []
        uploaded_files = request.httprequest.files.getlist("attachments")
        for f in uploaded_files:
            if not f or not f.filename:
                continue
            if f.mimetype not in ALLOWED_MIMETYPES:
                continue
            data = f.read()
            if len(data) > MAX_UPLOAD_BYTES:
                continue
            att = Attachment.create(
                {
                    "name": f.filename,
                    "datas": base64.b64encode(data),
                    "res_model": "maintenance.request",
                    "res_id": request_id,
                    "mimetype": f.mimetype,
                }
            )
            attachment_ids.append(att.id)

        # Post message as the user
        maint_req.sudo().message_post(
            body=body,
            message_type="comment",
            subtype_xmlid="mail.mt_comment",
            author_id=user.partner_id.id,
            attachment_ids=[(4, aid) for aid in attachment_ids],
        )

        return request.redirect(f"/tickets/{request_id}")
