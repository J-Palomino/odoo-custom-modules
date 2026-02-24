import logging
from datetime import date

from odoo import http
from odoo.http import request, Response

_logger = logging.getLogger(__name__)
_logger.warning("*** MINT MAINT CONTROLLER FILE LOADED, __name__=%s ***", __name__)

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

    @http.route("/maintenance/ping", type="http", auth="none", cors="*", csrf=False)
    def ping(self, **kw):
        """Simple test endpoint - no website, no auth, no CSRF."""
        return Response("pong", content_type="text/plain")

    @http.route("/maintenance/test", type="http", auth="public", website=True)
    def test_page(self, **kw):
        """Test website route with minimal template."""
        return request.render("mint_maintenance_form.request_form", self._form_context())

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

        schedule_date = post.get("schedule_date")
        if schedule_date:
            vals["schedule_date"] = schedule_date

        try:
            request.env["maintenance.request"].sudo().create(vals)
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


# ── Diagnostic logging (runs at import time, after class definition) ──
_logger.warning("*** Controller __module__ = %s ***", MaintenanceFormController.__module__)
_logger.warning("*** Controller MRO = %s ***", [c.__name__ for c in MaintenanceFormController.__mro__])

# Check if @http.route stored routing info on methods
for _name in ('ping', 'test_page', 'maintenance_form'):
    _method = getattr(MaintenanceFormController, _name, None)
    if _method:
        _routing = getattr(_method, 'routing', None)
        _original = getattr(_method, 'original_routing', None)
        _logger.warning("*** Method %s: routing=%s, original_routing=%s ***", _name, _routing, _original)
    else:
        _logger.warning("*** Method %s NOT FOUND ***", _name)

# List all Controller subclasses
try:
    _all_ctrl = type.__subclasses__(http.Controller)
    _logger.warning("*** Total Controller subclasses: %d ***", len(_all_ctrl))
    _logger.warning("*** Controller subclass names: %s ***",
                     [f"{c.__name__} ({c.__module__})" for c in _all_ctrl[:20]])
except Exception as _e:
    _logger.warning("*** Failed to list Controller subclasses: %s ***", _e)
