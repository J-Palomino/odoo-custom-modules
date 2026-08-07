import base64
import hashlib
import json
import logging
from collections import OrderedDict
from datetime import date, timedelta
from html import escape as html_escape

from markupsafe import Markup
from odoo import fields, http
from odoo.http import request

_logger = logging.getLogger(__name__)

MAX_UPLOAD_FILES = 5
MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB per file
ALLOWED_MIMETYPES = {
    # Images
    "image/jpeg", "image/png", "image/gif", "image/webp",
    "image/heic", "image/heif", "image/bmp", "image/tiff",
    # Documents
    "application/pdf",
    # Spreadsheets (xlsx / xls / csv) — requesters often attach data exports
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel",
    "text/csv",
    "application/csv",
    # Word docs / plain text
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/msword",
    "text/plain",
}

IT_TEAM_ID = 2
MINT_TECH_TEAM_ID = 4
MINT_TECH_TEAM_IDS = [IT_TEAM_ID, MINT_TECH_TEAM_ID]
INTERNAL_MAINTENANCE_TEAM_ID = 1
GRAPHICS_TEAM_ID = 12
DUTCHIE_TEAM_ID = 16
WEBSITES_TEAM_ID = 14
NEW_REQUEST_STAGE = 1

# How long an identical submission is treated as a duplicate rather than a
# new report.  Long enough to absorb double-clicks and browser POST retries
# on slow multipart uploads; short enough that a genuine re-report later in
# the day still opens its own ticket.
DUPLICATE_WINDOW_SECONDS = 600

# Category-based team routing: when a ticket's equipment belongs to one of
# these categories, override the assigned team regardless of which form
# submitted it.
CATEGORY_TEAM_OVERRIDES = {
    "Websites": WEBSITES_TEAM_ID,
}

# eLearning course that must be completed before accessing Fix-It forms.
# Set to None to disable the requirement.
FIXIT_COURSE_ID = 62

PRIORITY_OPTIONS = [
    ("0", "Very Low"),
    ("1", "Low"),
    ("2", "Normal"),
    ("3", "High"),
]


class MaintenanceFormController(http.Controller):

    def _course_status(self):
        """Return a context dict describing the current user's Fix-It
        training status.  The training course is *encouraged*, not
        required — users may always submit a ticket.  When the user
        hasn't completed the course, ``training_incomplete`` is True and
        a banner linking to the course is shown on the forms.

        Returns a dict suitable for merging into a render context:
            {"training_incomplete": bool, "training_course_url": str}
        """
        ctx = {"training_incomplete": False, "training_course_url": ""}
        if not FIXIT_COURSE_ID:
            return ctx
        user = request.env.user
        # Internal / admin users never see the banner
        if user.has_group("base.group_system"):
            return ctx
        partner = user.partner_id
        completed = (
            request.env["slide.channel.partner"]
            .sudo()
            .search_count([
                ("channel_id", "=", FIXIT_COURSE_ID),
                ("partner_id", "=", partner.id),
                ("member_status", "=", "completed"),
            ])
        )
        if completed:
            return ctx
        course = request.env["slide.channel"].sudo().browse(FIXIT_COURSE_ID)
        ctx["training_incomplete"] = True
        ctx["training_course_url"] = course.website_url or f"/slides/{FIXIT_COURSE_ID}"
        return ctx

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
        ctx.update(self._course_status())
        ctx.update(extra)
        # Banner for the GET that follows a submission redirect (PRG).
        if not ctx.get("success"):
            banner = self._submitted_banner()
            if banner:
                ctx["success"] = banner
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
                    "Please upload images, PDFs, or Office documents "
                    "(Excel, Word, CSV, text)."
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

    def _submission_key(self, vals):
        """Stable idempotency key for a submitted form payload.

        Hashed rather than compared field by field, because the obvious
        approach does not work: `description` is an HTML field with
        sanitize=True, so Odoo rewrites the markup on write and the stored
        string is never byte-identical to what was posted.  A `description =`
        term therefore silently never matches — which is exactly how the
        earlier guard came to be inert.  Hashing what the user supplied, and
        storing the digest on the ticket, avoids HTML normalisation entirely.

        The submitter is identified by uid, or by the submitted email for a
        public visitor.  Deliberately NOT by owner_user_id: that field is
        computed from employee_id, so the value the controller puts in vals is
        discarded on create and the column is empty on 51% of production
        web-intake tickets (89 of 174).  MR-1194 and MR-1195 — the same person
        submitting the same form five minutes apart — stored False and 1985
        respectively, so an owner_user_id term could never have linked them.
        """
        public_uid = request.env.ref("base.public_user").id
        uid = request.env.uid
        if uid and uid != public_uid:
            submitter = f"uid:{uid}"
        else:
            submitter = "email:" + (vals.get("email_cc") or "").strip().lower()
        seed = "|".join([
            submitter,
            str(vals.get("maintenance_team_id") or ""),
            str(vals.get("company_id") or ""),
            vals.get("description") or "",
        ])
        return hashlib.sha256(seed.encode()).hexdigest()

    def _find_recent_duplicate(self, vals):
        """Return an identical request submitted in the last few minutes, if any.

        Guards against the three ways one report becomes many tickets:
        an impatient double-click while a large attachment is still
        uploading, a browser/proxy retry of the POST, and "Confirm Form
        Resubmission" on refresh.

        Matched on the stored submission key, never on `name`: a Maintenance
        UI automation rewrites `name` to "MR-<id> - ..." immediately after
        create — it embeds the record's own id, so no two rows can ever share
        a title and a name-keyed guard can never fire.
        """
        if not vals.get("description"):
            return None

        key = vals.get("x_submission_key") or self._submission_key(vals)

        # Serialise identical submissions against each other.  A plain search
        # cannot see a row another worker has created but not yet committed,
        # so without this two near-simultaneous clicks both miss and both
        # create.  The lock is held until this transaction ends, so the second
        # request waits and then finds the first one's ticket.
        lock_key = int.from_bytes(bytes.fromhex(key)[:8], "big", signed=True)
        request.env.cr.execute("SELECT pg_advisory_xact_lock(%s)", (lock_key,))

        cutoff = fields.Datetime.to_string(
            fields.Datetime.now() - timedelta(seconds=DUPLICATE_WINDOW_SECONDS)
        )
        domain = [
            ("x_submission_key", "=", key),
            ("create_date", ">=", cutoff),
            # A ticket already resolved inside the window is not the same
            # report.  If it was closed and the user is submitting again, they
            # are telling us it is not actually fixed — that deserves its own
            # ticket rather than being folded into the closed one.
            ("stage_id.done", "=", False),
        ]

        existing = request.env["maintenance.request"].sudo().search(
            domain, limit=1, order="create_date desc"
        )
        return existing or None

    def _success_redirect(self, maint_req, ctx, success_msg):
        """POST/Redirect/GET.

        Without this the browser sits on a POST result, so refresh /
        back-forward re-submits the whole form and files another ticket.
        The per-form wording is flashed through the session; the redirect
        target only carries the ticket id.
        """
        request.session["mint_form_success"] = success_msg
        path = ctx.get("form_action") or request.httprequest.path
        return request.redirect(f"{path}?submitted={maint_req.id}")

    def _submitted_banner(self):
        """Success message for the GET that follows a submission redirect.

        Only confirms a ticket the current user actually filed, so a guessed
        `?submitted=` id cannot be used to probe other people's requests.
        """
        raw = request.httprequest.args.get("submitted")
        if not raw or not raw.isdigit():
            return None
        maint_req = request.env["maintenance.request"].sudo().browse(int(raw))
        if not maint_req.exists():
            return None
        flash = request.session.pop("mint_form_success", None)
        user = request.env.user
        user_email = (user.email or "").strip().lower()
        owned = (
            maint_req.owner_user_id.id == user.id
            or maint_req.create_uid.id == user.id
            or (user_email and (maint_req.email_cc or "").strip().lower() == user_email)
        )
        if not owned:
            return None
        base = flash or "Your request has been submitted successfully!"
        return f"{base} Reference: {maint_req.name}"

    def _create_request(self, vals, valid_files, template, ctx, success_msg):
        """Create maintenance.request + attachments. Returns rendered response."""
        try:
            # Mark how the ticket came in so it's distinguishable from
            # agent-created (phone/sms) and Odoo-direct tickets.
            vals.setdefault("x_intake_channel", "web")

            # Idempotency: a repeat of an identical submission resolves to the
            # ticket already created rather than a second one.  Re-uploading
            # the same attachments is skipped along with it.  The key is
            # stamped on the row so the next submission has something exact
            # and indexed to match against.
            vals["x_submission_key"] = self._submission_key(vals)
            duplicate = self._find_recent_duplicate(vals)
            if duplicate:
                _logger.info(
                    "Duplicate submission suppressed — resolving to existing "
                    "request %s (%s)", duplicate.id, duplicate.name,
                )
                return self._success_redirect(duplicate, ctx, success_msg)

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

            return self._success_redirect(maint_req, ctx, success_msg)
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

        # Category-based team routing override
        if category in CATEGORY_TEAM_OVERRIDES:
            assigned_team = CATEGORY_TEAM_OVERRIDES[category]

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

        if company_id:
            vals["company_id"] = int(company_id)

        if equipment_id:
            equip = request.env["maintenance.equipment"].sudo().browse(equipment_id)
            # Ensure equipment company matches the request company to avoid
            # multi-company constraint violations.  Shared equipment (company_id
            # = False) is safe; a mismatch must be resolved before linking.
            if equip.company_id and company_id and equip.company_id.id != int(company_id):
                # Equipment belongs to a different company — clear it so
                # the request is created without the equipment link.
                _logger.info(
                    "Skipping equipment_id %s (%s) — belongs to company %s, "
                    "request is for company %s",
                    equipment_id, equip.name,
                    equip.company_id.id, company_id,
                )
            else:
                vals["equipment_id"] = equipment_id
                if equip.technician_user_id:
                    vals["user_id"] = equip.technician_user_id.id
            # Route to a different team based on equipment category
            if equip.category_id and equip.category_id.name in CATEGORY_TEAM_OVERRIDES:
                vals["maintenance_team_id"] = CATEGORY_TEAM_OVERRIDES[equip.category_id.name]

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
        return request.render(
            "mint_maintenance_form.routing_page", self._course_status()
        )

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
            "form_title": "Mint Technology Request",
            "form_subtitle": "Submit a Mint Technology request for websites, e-commerce, apps, AI/LLMs, chat, or new software.",
            "form_action": "/engineering-requests",
        }

        if request.httprequest.method == "GET":
            prefill, logged_in = self._get_user_prefill()
            return request.render(
                template,
                self._form_context(
                    MINT_TECH_TEAM_ID,
                    form_values=prefill,
                    is_logged_in=logged_in,
                    **form_ctx,
                ),
            )

        return self._handle_form_post(
            team_id=MINT_TECH_TEAM_ID,
            template=template,
            success_msg="Your Mint Technology request has been submitted successfully!",
            default_title="Mint Technology Request",
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
    # /vendor-requests — 3rd-party vendor form (Dutchie, etc.)
    # ------------------------------------------------------------------
    @http.route(
        ["/vendor-requests", "/dutchie-requests"],
        type="http",
        auth="user",
        website=True,
        methods=["GET", "POST"],
    )
    def vendor_request_form(self, **post):
        template = "mint_maintenance_form.engineering_request_form"
        form_ctx = {
            "form_title": "Vendor Request",
            "form_subtitle": "Submit a request for third-party vendor issues — Dutchie POS, menu sync, online ordering, and other vendor platforms.",
            "form_action": "/vendor-requests",
        }

        if request.httprequest.method == "GET":
            prefill, logged_in = self._get_user_prefill()
            return request.render(
                template,
                self._form_context(
                    DUTCHIE_TEAM_ID,
                    form_values=prefill,
                    is_logged_in=logged_in,
                    **form_ctx,
                ),
            )

        return self._handle_form_post(
            team_id=DUTCHIE_TEAM_ID,
            template=template,
            success_msg="Your vendor request has been submitted successfully!",
            default_title="Vendor Request",
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
                    INTERNAL_MAINTENANCE_TEAM_ID,
                    form_values=prefill,
                    is_logged_in=logged_in,
                ),
            )

        return self._handle_form_post(
            team_id=INTERNAL_MAINTENANCE_TEAM_ID,
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
            # message_post expects a plain list of attachment IDs, NOT write-command
            # tuples. Passing [(4, aid), ...] raises "unhashable type: 'list'" /
            # "should receive attachments records as a list of IDs" (see MR #505).
            attachment_ids=attachment_ids,
        )

        return request.redirect(f"/tickets/{request_id}")
