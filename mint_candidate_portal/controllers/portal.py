from collections import OrderedDict

from odoo import _, http
from odoo.exceptions import AccessError, MissingError
from odoo.http import request

from odoo.addons.portal.controllers.portal import CustomerPortal, pager as portal_pager


class CandidatePortal(CustomerPortal):
    """Candidate-facing portal: shows the user's own hr.applicant records.

    Distinct from mint_recruitment_portal which gates on Human Resources
    department and exposes /my/jobs + /my/applicants for HR staff.
    """

    # --- Portal home counter ---------------------------------------------

    def _prepare_home_portal_values(self, counters):
        values = super()._prepare_home_portal_values(counters)
        if "application_count" in counters:
            values["application_count"] = request.env["hr.applicant"].sudo().search_count(
                self._candidate_applicant_domain()
            )
        return values

    # --- Helpers ----------------------------------------------------------

    def _candidate_applicant_domain(self):
        partner = request.env.user.partner_id
        return [("partner_id", "=", partner.id)]

    def _get_applicant(self, applicant_id):
        applicant = request.env["hr.applicant"].sudo().browse(applicant_id)
        if not applicant.exists():
            raise MissingError(_("Application not found"))
        if applicant.partner_id.id != request.env.user.partner_id.id:
            raise AccessError(_("You do not have access to this application."))
        return applicant

    # --- List view --------------------------------------------------------

    @http.route(
        ["/my/applications", "/my/applications/page/<int:page>"],
        type="http",
        auth="user",
        website=True,
    )
    def portal_my_applications(self, page=1, sortby=None, filterby=None, **kw):
        searchbar_sortings = {
            "date": {"label": _("Newest"), "order": "create_date desc"},
            "stage": {"label": _("Stage"), "order": "stage_id asc"},
            "job": {"label": _("Job Position"), "order": "job_id asc"},
        }
        sortby = sortby or "date"
        order = searchbar_sortings[sortby]["order"]

        stages = request.env["hr.recruitment.stage"].sudo().search([], order="sequence")
        searchbar_filters = OrderedDict([
            ("all", {"label": _("All"), "domain": []}),
        ])
        for stage in stages:
            searchbar_filters[str(stage.id)] = {
                "label": stage.name,
                "domain": [("stage_id", "=", stage.id)],
            }
        filterby = filterby or "all"

        domain = self._candidate_applicant_domain() + searchbar_filters[filterby]["domain"]

        Applicant = request.env["hr.applicant"].sudo()
        total = Applicant.search_count(domain)
        pager = portal_pager(
            url="/my/applications",
            total=total,
            page=page,
            step=10,
            url_args={"sortby": sortby, "filterby": filterby},
        )
        applications = Applicant.search(domain, order=order, limit=10, offset=pager["offset"])

        values = self._prepare_portal_layout_values()
        values.update({
            "applications": applications,
            "page_name": "candidate_applications",
            "pager": pager,
            "default_url": "/my/applications",
            "searchbar_sortings": searchbar_sortings,
            "searchbar_filters": searchbar_filters,
            "sortby": sortby,
            "filterby": filterby,
        })
        return request.render("mint_candidate_portal.portal_my_applications", values)

    # --- Detail view ------------------------------------------------------

    @http.route(
        "/my/applications/<int:applicant_id>",
        type="http",
        auth="user",
        website=True,
    )
    def portal_application_detail(self, applicant_id, **kw):
        try:
            applicant = self._get_applicant(applicant_id)
        except (AccessError, MissingError):
            return request.redirect("/my/applications")

        values = self._prepare_portal_layout_values()
        values.update({
            "applicant": applicant,
            "stages": request.env["hr.recruitment.stage"].sudo().search([], order="sequence"),
            "page_name": "candidate_application_detail",
        })
        return request.render("mint_candidate_portal.portal_application_detail", values)

    # --- Post message to the applicant thread -----------------------------

    @http.route(
        "/my/applications/<int:applicant_id>/message",
        type="http",
        auth="user",
        website=True,
        methods=["POST"],
        csrf=True,
    )
    def portal_application_post_message(self, applicant_id, message="", **kw):
        try:
            applicant = self._get_applicant(applicant_id)
        except (AccessError, MissingError):
            return request.redirect("/my/applications")

        message = (message or "").strip()
        if message:
            applicant.sudo().message_post(
                body=message,
                author_id=request.env.user.partner_id.id,
                message_type="comment",
                subtype_xmlid="mail.mt_comment",
            )
        return request.redirect(f"/my/applications/{applicant_id}")
