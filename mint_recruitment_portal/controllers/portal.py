import base64
from collections import OrderedDict

from odoo import _, http
from odoo.exceptions import AccessError, MissingError
from odoo.http import request
from odoo.osv import expression

from odoo.addons.portal.controllers.portal import CustomerPortal
from odoo.addons.portal.controllers.portal import pager as portal_pager


class RecruitmentPortal(CustomerPortal):
    """Website portal for HR admins to manage job listings and applicants."""

    # ------------------------------------------------------------------
    # Portal home — dashboard counts
    # ------------------------------------------------------------------

    def _prepare_home_portal_values(self, counters):
        values = super()._prepare_home_portal_values(counters)
        if not self._is_recruitment_user():
            return values
        if "job_count" in counters:
            values["job_count"] = (
                request.env["hr.job"].sudo().search_count(self._job_domain())
            )
        if "applicant_count" in counters:
            values["applicant_count"] = (
                request.env["hr.applicant"].sudo().search_count(self._applicant_domain())
            )
        return values

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _is_recruitment_user(self):
        """Check if the current user is an employee in the Human Resources department."""
        employee = request.env["hr.employee"].sudo().search(
            [("user_id", "=", request.env.uid), ("department_id.name", "=", "Human Resources")],
            limit=1,
        )
        return bool(employee)

    def _check_recruitment_access(self):
        if not self._is_recruitment_user():
            raise AccessError(_("You do not have access to recruitment management."))

    def _job_domain(self):
        return []

    def _applicant_domain(self):
        return [("is_pool_applicant", "=", False)]

    # ------------------------------------------------------------------
    # Job Positions — List
    # ------------------------------------------------------------------

    @http.route(
        ["/my/jobs", "/my/jobs/page/<int:page>"],
        type="http",
        auth="user",
        website=True,
    )
    def portal_my_jobs(self, page=1, sortby=None, filterby=None, search=None, **kw):
        self._check_recruitment_access()

        searchbar_sortings = {
            "name": {"label": _("Name"), "order": "name asc"},
            "department": {"label": _("Department"), "order": "department_id asc"},
            "published": {"label": _("Published"), "order": "website_published desc"},
        }
        searchbar_filters = OrderedDict([
            ("all", {"label": _("All"), "domain": []}),
            ("published", {"label": _("Published"), "domain": [("website_published", "=", True)]}),
            ("unpublished", {"label": _("Unpublished"), "domain": [("website_published", "=", False)]}),
        ])

        sortby = sortby or "name"
        filterby = filterby or "all"
        order = searchbar_sortings[sortby]["order"]
        domain = expression.AND([
            self._job_domain(),
            searchbar_filters[filterby]["domain"],
        ])
        if search:
            domain = expression.AND([domain, [("name", "ilike", search)]])

        Job = request.env["hr.job"].sudo()
        total = Job.search_count(domain)
        pager = portal_pager(
            url="/my/jobs",
            total=total,
            page=page,
            step=20,
            url_args={"sortby": sortby, "filterby": filterby, "search": search or ""},
        )
        jobs = Job.search(domain, order=order, limit=20, offset=pager["offset"])

        values = self._prepare_portal_layout_values()
        values.update({
            "jobs": jobs,
            "page_name": "recruitment_jobs",
            "pager": pager,
            "default_url": "/my/jobs",
            "searchbar_sortings": searchbar_sortings,
            "searchbar_filters": searchbar_filters,
            "sortby": sortby,
            "filterby": filterby,
            "search_in": search or "",
        })
        return request.render("mint_recruitment_portal.portal_my_jobs", values)

    # ------------------------------------------------------------------
    # Job Positions — Detail / Edit
    # ------------------------------------------------------------------

    @http.route(
        "/my/jobs/<int:job_id>",
        type="http",
        auth="user",
        website=True,
    )
    def portal_job_detail(self, job_id, **kw):
        self._check_recruitment_access()
        try:
            job = request.env["hr.job"].sudo().browse(job_id)
            if not job.exists():
                raise MissingError(_("Job not found"))
        except (AccessError, MissingError):
            return request.redirect("/my/jobs")

        stages = request.env["hr.recruitment.stage"].sudo().search([], order="sequence")
        applicants = request.env["hr.applicant"].sudo().search([
            ("job_id", "=", job.id),
            ("is_pool_applicant", "=", False),
        ], order="create_date desc", limit=20)

        values = self._prepare_portal_layout_values()
        values.update({
            "job": job,
            "stages": stages,
            "applicants": applicants,
            "page_name": "recruitment_job_detail",
        })
        return request.render("mint_recruitment_portal.portal_job_detail", values)

    # ------------------------------------------------------------------
    # Job — Toggle publish
    # ------------------------------------------------------------------

    @http.route(
        "/my/jobs/<int:job_id>/toggle_publish",
        type="http",
        auth="user",
        website=True,
        methods=["POST"],
    )
    def portal_job_toggle_publish(self, job_id, **kw):
        self._check_recruitment_access()
        job = request.env["hr.job"].sudo().browse(job_id)
        if job.exists():
            job.website_published = not job.website_published
        return request.redirect(f"/my/jobs/{job_id}")

    # ------------------------------------------------------------------
    # Job — Update details
    # ------------------------------------------------------------------

    @http.route(
        "/my/jobs/<int:job_id>/update",
        type="http",
        auth="user",
        website=True,
        methods=["POST"],
    )
    def portal_job_update(self, job_id, **kw):
        self._check_recruitment_access()
        job = request.env["hr.job"].sudo().browse(job_id)
        if not job.exists():
            return request.redirect("/my/jobs")

        vals = {}
        if "name" in kw and kw["name"].strip():
            vals["name"] = kw["name"].strip()
        if "no_of_recruitment" in kw:
            try:
                vals["no_of_recruitment"] = max(0, int(kw["no_of_recruitment"]))
            except (ValueError, TypeError):
                pass
        if "website_description" in kw:
            vals["website_description"] = kw["website_description"]

        if vals:
            job.write(vals)
        return request.redirect(f"/my/jobs/{job_id}")

    # ------------------------------------------------------------------
    # Applicants — List
    # ------------------------------------------------------------------

    @http.route(
        ["/my/applicants", "/my/applicants/page/<int:page>"],
        type="http",
        auth="user",
        website=True,
    )
    def portal_my_applicants(self, page=1, sortby=None, filterby=None, search=None, **kw):
        self._check_recruitment_access()

        searchbar_sortings = {
            "date": {"label": _("Newest"), "order": "create_date desc"},
            "name": {"label": _("Name"), "order": "partner_name asc"},
            "stage": {"label": _("Stage"), "order": "stage_id asc"},
            "job": {"label": _("Job Position"), "order": "job_id asc"},
        }
        stages = request.env["hr.recruitment.stage"].sudo().search([], order="sequence")
        searchbar_filters = OrderedDict([
            ("all", {"label": _("All Stages"), "domain": []}),
        ])
        for stage in stages:
            searchbar_filters[str(stage.id)] = {
                "label": stage.name,
                "domain": [("stage_id", "=", stage.id)],
            }

        sortby = sortby or "date"
        filterby = filterby or "all"
        order = searchbar_sortings[sortby]["order"]
        domain = expression.AND([
            self._applicant_domain(),
            searchbar_filters[filterby]["domain"],
        ])
        if search:
            domain = expression.AND([domain, [
                "|", "|",
                ("partner_name", "ilike", search),
                ("email_from", "ilike", search),
                ("job_id.name", "ilike", search),
            ]])

        Applicant = request.env["hr.applicant"].sudo()
        total = Applicant.search_count(domain)
        pager = portal_pager(
            url="/my/applicants",
            total=total,
            page=page,
            step=20,
            url_args={"sortby": sortby, "filterby": filterby, "search": search or ""},
        )
        applicants = Applicant.search(domain, order=order, limit=20, offset=pager["offset"])

        values = self._prepare_portal_layout_values()
        values.update({
            "applicants": applicants,
            "page_name": "recruitment_applicants",
            "pager": pager,
            "default_url": "/my/applicants",
            "searchbar_sortings": searchbar_sortings,
            "searchbar_filters": searchbar_filters,
            "sortby": sortby,
            "filterby": filterby,
            "search_in": search or "",
            "stages": stages,
        })
        return request.render("mint_recruitment_portal.portal_my_applicants", values)

    # ------------------------------------------------------------------
    # Applicant — Detail
    # ------------------------------------------------------------------

    @http.route(
        "/my/applicants/<int:applicant_id>",
        type="http",
        auth="user",
        website=True,
    )
    def portal_applicant_detail(self, applicant_id, **kw):
        self._check_recruitment_access()
        try:
            applicant = request.env["hr.applicant"].sudo().browse(applicant_id)
            if not applicant.exists():
                raise MissingError(_("Applicant not found"))
        except (AccessError, MissingError):
            return request.redirect("/my/applicants")

        stages = request.env["hr.recruitment.stage"].sudo().search([], order="sequence")

        values = self._prepare_portal_layout_values()
        values.update({
            "applicant": applicant,
            "stages": stages,
            "page_name": "recruitment_applicant_detail",
        })
        return request.render("mint_recruitment_portal.portal_applicant_detail", values)

    # ------------------------------------------------------------------
    # Applicant — Change stage
    # ------------------------------------------------------------------

    @http.route(
        "/my/applicants/<int:applicant_id>/stage",
        type="http",
        auth="user",
        website=True,
        methods=["POST"],
    )
    def portal_applicant_stage(self, applicant_id, stage_id=None, **kw):
        self._check_recruitment_access()
        applicant = request.env["hr.applicant"].sudo().browse(applicant_id)
        if applicant.exists() and stage_id:
            try:
                applicant.stage_id = int(stage_id)
            except (ValueError, TypeError):
                pass
        return request.redirect(f"/my/applicants/{applicant_id}")

    # ------------------------------------------------------------------
    # Applicant — Add note
    # ------------------------------------------------------------------

    @http.route(
        "/my/applicants/<int:applicant_id>/note",
        type="http",
        auth="user",
        website=True,
        methods=["POST"],
    )
    def portal_applicant_note(self, applicant_id, note=None, **kw):
        self._check_recruitment_access()
        applicant = request.env["hr.applicant"].sudo().browse(applicant_id)
        if applicant.exists() and note and note.strip():
            applicant.message_post(
                body=note.strip(),
                message_type="comment",
                subtype_xmlid="mail.mt_note",
            )
        return request.redirect(f"/my/applicants/{applicant_id}")

    # ------------------------------------------------------------------
    # Applicant — Download resume
    # ------------------------------------------------------------------

    @http.route(
        "/my/applicants/<int:applicant_id>/resume/<int:attachment_id>",
        type="http",
        auth="user",
        website=True,
    )
    def portal_applicant_resume(self, applicant_id, attachment_id, **kw):
        self._check_recruitment_access()
        applicant = request.env["hr.applicant"].sudo().browse(applicant_id)
        if not applicant.exists():
            return request.redirect("/my/applicants")

        attachment = request.env["ir.attachment"].sudo().browse(attachment_id)
        if not attachment.exists() or attachment.res_id != applicant_id:
            return request.redirect(f"/my/applicants/{applicant_id}")

        content = base64.b64decode(attachment.datas)
        return request.make_response(
            content,
            headers=[
                ("Content-Type", attachment.mimetype or "application/octet-stream"),
                ("Content-Disposition", f'attachment; filename="{attachment.name}"'),
            ],
        )
