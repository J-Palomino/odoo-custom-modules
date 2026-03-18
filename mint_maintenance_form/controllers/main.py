import base64
import json
import logging
from collections import OrderedDict
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
ENGINEERING_TEAM_ID = 4
ENGINEERING_TEAM_IDS = [IT_TEAM_ID, ENGINEERING_TEAM_ID]
FACILITIES_TEAM_ID = 11
GRAPHICS_TEAM_ID = 12
NEW_REQUEST_STAGE = 1

PRIORITY_OPTIONS = [
    ("0", "Very Low"),
    ("1", "Low"),
    ("2", "Normal"),
    ("3", "High"),
]


class MaintenanceFormController(http.Controller):

    def _check_request_access(self, maint_req, user):
        """Return True if user owns this request or is on the assigned team."""
        if not maint_req.exists():
            return False
        if maint_req.owner_user_id and maint_req.owner_user_id.id == user.id:
            return True
        if user.email and user.email.lower() in (maint_req.email_cc or "").lower():
            return True
        # Team members can view their team's requests
        if maint_req.maintenance_team_id:
            if user.id in maint_req.maintenance_team_id.member_ids.ids:
                return True
        return False

    def _get_team_equipment(self, team_ids):
        """Query equipment for one or more teams, grouped by category.

        Args:
            team_ids: int or list of ints — maintenance team ID(s)

        Returns:
            equipment_types: list of (category_name, category_name) for the dropdown
            subtype_options: dict of {category_name: [(equip_id, equip_name), ...]}
                             only for categories with >1 equipment
        """
        if isinstance(team_ids, int):
            team_ids = [team_ids]
        Equipment = request.env["maintenance.equipment"].sudo()
        records = Equipment.search(
            [("maintenance_team_id", "in", team_ids)],
            order="category_id, name",
        )

        categories = OrderedDict()
        for equip in records:
            cat_name = equip.category_id.name if equip.category_id else "Other"
            if cat_name not in categories:
                categories[cat_name] = []
            categories[cat_name].append((str(equip.id), equip.name))

        equipment_types = [(cat, cat) for cat in categories]

        subtype_options = {}
        for cat_name, equips in categories.items():
            if len(equips) > 1:
                subtype_options[cat_name] = equips

        return equipment_types, subtype_options

    def _form_context(self, team_id, **extra):
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

        equipment_types, subtype_options = self._get_team_equipment(team_id)

        ctx = {
            "companies": company_list,
            "states": sorted(states),
            "equipment_types": equipment_types,
            "subtype_options_json": Markup(json.dumps(subtype_options)),
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

            # Subscribe team members and notify
            try:
                team = maint_req.maintenance_team_id
                if team and team.member_ids:
                    partner_ids = team.member_ids.mapped("partner_id").ids
                    maint_req.message_subscribe(partner_ids=partner_ids)
                maint_req.message_post(
                    body=f"New request submitted: {vals.get('name', '')}",
                    message_type="notification",
                    subtype_xmlid="mail.mt_comment",
                )
            except Exception:
                _logger.warning("Failed to send notification for request %s", maint_req.id)

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

    def _resolve_equipment(self, team_ids, category_name, subtype_id):
        """Resolve equipment_id and label from form POST values.

        Args:
            team_ids: int or list of ints — maintenance team ID(s)
            category_name: selected category (equipment_type field)
            subtype_id: selected equipment ID string (subtype field), or empty

        Returns:
            (equipment_id, equipment_label) tuple
        """
        if isinstance(team_ids, int):
            team_ids = [team_ids]
        Equipment = request.env["maintenance.equipment"].sudo()

        if subtype_id:
            equip = Equipment.browse(int(subtype_id))
            if equip.exists():
                return equip.id, f"{category_name} - {equip.name}"
            return None, category_name

        # Single-equipment category — look up by team + category name
        equip = Equipment.search([
            ("maintenance_team_id", "in", team_ids),
            ("category_id.name", "=", category_name),
        ], limit=1)
        if equip:
            return equip.id, category_name
        return None, category_name

    def _handle_form_post(self, team_id, template, success_msg, default_title, extra_ctx=None, **post):
        """Shared POST handler for Engineering, IT, and Facilities forms.

        team_id can be an int or list of ints. When creating the request,
        the first ID in the list is used as the assigned team.
        """
        ctx = self._form_context(team_id, form_values=post, **(extra_ctx or {}))

        submitter_name = post.get("submitter_name", "").strip()
        submitter_email = post.get("submitter_email", "").strip()
        category = post.get("equipment_type", "").strip()
        subtype = post.get("subtype", "").strip()
        state = post.get("state", "").strip()
        description = post.get("description", "").strip()
        priority = post.get("priority", "2")

        # Validation
        error = self._validate_common_fields(post)
        if error:
            ctx["error"] = error
            return request.render(template, ctx)
        if not state:
            ctx["error"] = "Please select a state."
            return request.render(template, ctx)
        if not post.get("company_id", "").strip():
            ctx["error"] = "Please select a store / location."
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

        # Resolve equipment
        equipment_id, equipment_label = self._resolve_equipment(
            team_id, category, subtype
        )

        # Auto-generate request title
        title = store_name or default_title
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

        # When team_id is a list, use the first for the created record
        assigned_team = team_id[0] if isinstance(team_id, list) else team_id

        vals = {
            "name": title,
            "description": desc_html,
            "maintenance_type": "corrective",
            "priority": priority,
            "maintenance_team_id": assigned_team,
            "stage_id": NEW_REQUEST_STAGE,
            "request_date": date.today(),
            "email_cc": submitter_email,
        }

        if equipment_id:
            vals["equipment_id"] = equipment_id
            equip = request.env["maintenance.equipment"].sudo().browse(equipment_id)
            if equip.technician_user_id:
                vals["user_id"] = equip.technician_user_id.id

        if company_id:
            vals["company_id"] = int(company_id)

        if request.env.uid and request.env.uid != request.env.ref("base.public_user").id:
            vals["owner_user_id"] = request.env.uid

        # Validate uploaded files
        valid_files, error_resp = self._validate_uploads(template, ctx)
        if error_resp:
            return error_resp

        return self._create_request(vals, valid_files, template, ctx, success_msg)

    # ------------------------------------------------------------------
    # /fixit — routing page
    # ------------------------------------------------------------------
    @http.route(
        "/fixit",
        type="http",
        auth="user",
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
        auth="user",
        website=True,
        methods=["GET", "POST"],
    )
    def it_request_form(self, **post):
        template = "mint_maintenance_form.engineering_request_form"
        form_ctx = {
            "form_title": "IT Request",
            "form_subtitle": "Submit an IT request. This creates a ticket assigned to the IT team.",
            "form_action": "/it-requests",
        }

        if request.httprequest.method == "GET":
            prefill, logged_in = self._get_user_prefill()
            return request.render(
                template,
                self._form_context(
                    IT_TEAM_ID,
                    form_values=prefill,
                    is_logged_in=logged_in,
                    **form_ctx,
                ),
            )

        return self._handle_form_post(
            team_id=IT_TEAM_ID,
            template=template,
            success_msg="Your IT request has been submitted successfully!",
            default_title="IT Request",
            extra_ctx=form_ctx,
            **post,
        )

    # ------------------------------------------------------------------
    # /engineering-requests — Engineering / software form
    # ------------------------------------------------------------------
    @http.route(
        "/engineering-requests",
        type="http",
        auth="user",
        website=True,
        methods=["GET", "POST"],
    )
    def engineering_request_form(self, **post):
        template = "mint_maintenance_form.engineering_request_form"
        form_ctx = {
            "form_title": "Engineering Request",
            "form_subtitle": "Submit an engineering request for websites, e-commerce, apps, AI/LLMs, chat, or new software.",
            "form_action": "/engineering-requests",
        }

        if request.httprequest.method == "GET":
            prefill, logged_in = self._get_user_prefill()
            return request.render(
                template,
                self._form_context(
                    ENGINEERING_TEAM_ID,
                    form_values=prefill,
                    is_logged_in=logged_in,
                    **form_ctx,
                ),
            )

        return self._handle_form_post(
            team_id=ENGINEERING_TEAM_ID,
            template=template,
            success_msg="Your engineering request has been submitted successfully!",
            default_title="Engineering Request",
            extra_ctx=form_ctx,
            **post,
        )

    # ------------------------------------------------------------------
    # /graphics-requests — Graphics / signage / print form
    # ------------------------------------------------------------------
    @http.route(
        "/graphics-requests",
        type="http",
        auth="user",
        website=True,
        methods=["GET", "POST"],
    )
    def graphics_request_form(self, **post):
        template = "mint_maintenance_form.engineering_request_form"
        form_ctx = {
            "form_title": "Graphics Request",
            "form_subtitle": "Submit a graphics request for signage, menu boards, print materials, vehicle wraps, or digital assets.",
            "form_action": "/graphics-requests",
        }

        if request.httprequest.method == "GET":
            prefill, logged_in = self._get_user_prefill()
            return request.render(
                template,
                self._form_context(
                    GRAPHICS_TEAM_ID,
                    form_values=prefill,
                    is_logged_in=logged_in,
                    **form_ctx,
                ),
            )

        return self._handle_form_post(
            team_id=GRAPHICS_TEAM_ID,
            template=template,
            success_msg="Your graphics request has been submitted successfully!",
            default_title="Graphics Request",
            extra_ctx=form_ctx,
            **post,
        )

    # ------------------------------------------------------------------
    # /maintenance-requests — Facilities maintenance form
    # ------------------------------------------------------------------
    @http.route(
        "/maintenance-requests",
        type="http",
        auth="user",
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
                    FACILITIES_TEAM_ID,
                    form_values=prefill,
                    is_logged_in=logged_in,
                ),
            )

        return self._handle_form_post(
            team_id=FACILITIES_TEAM_ID,
            template=template,
            success_msg="Your maintenance request has been submitted successfully!",
            default_title="Maintenance Request",
            **post,
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
        user_teams = request.env["maintenance.team"].sudo().search(
            [("member_ids", "in", [user.id])]
        )
        if user_teams:
            domain = [
                ("stage_id.done", "!=", True),
                "|", "|",
                ("owner_user_id", "=", user.id),
                ("email_cc", "ilike", user.email or ""),
                ("maintenance_team_id", "in", user_teams.ids),
            ]
        else:
            domain = [
                ("stage_id.done", "!=", True),
                "|",
                ("owner_user_id", "=", user.id),
                ("email_cc", "ilike", user.email or ""),
            ]
        requests_list = MaintRequest.search(
            domain,
            order="request_date desc, id desc",
            limit=50,
        )

        items = []
        for r in requests_list:
            team_name = r.maintenance_team_id.name if r.maintenance_team_id else ""
            items.append({
                "id": r.id,
                "name": r.name or "",
                "stage": r.stage_id.name if r.stage_id else "New",
                "priority": priority_labels.get(r.priority, "Normal"),
                "request_date": r.request_date,
                "company": r.company_id.name if r.company_id else "",
                "equipment": r.equipment_id.name if r.equipment_id else "",
                "request_type": team_name or "Other",
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
        team_name = maint_req.maintenance_team_id.name if maint_req.maintenance_team_id else "Other"

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
                "request_type": team_name,
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
