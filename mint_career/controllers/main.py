from odoo import http

from odoo.addons.website_hr_recruitment.controllers.main import WebsiteHrRecruitment


class MintCareer(WebsiteHrRecruitment):
    """Mirror Odoo 19 website_hr_recruitment routes at /careers.

    Upstream routes (verified against odoo/odoo @ 19.0):
        GET  /jobs[?country_id=&department_id=&office_id=&...]
        GET  /jobs/page/<int>
        GET  /jobs/detail/<hr.job>
        GET  /jobs/<hr.job>
        GET  /jobs/apply/<hr.job>
        POST /jobs/add  (jsonrpc, auth=user — HR admin creates a job)

    Each /careers route below calls the matching super() handler so behaviour
    stays in lockstep with upstream. URL generation in templates still emits
    /jobs/* links; rewriting those is handled in Phase 3 (theme work).
    """

    @http.route(
        ["/careers", "/careers/page/<int:page>"],
        type="http",
        auth="public",
        website=True,
        sitemap=True,
    )
    def careers(self, **kwargs):
        return self.jobs(**kwargs)

    @http.route(
        '/careers/detail/<model("hr.job"):job>',
        type="http",
        auth="public",
        website=True,
        sitemap=True,
    )
    def careers_detail(self, job, **kwargs):
        return self.jobs_detail(job, **kwargs)

    @http.route(
        '/careers/<model("hr.job"):job>',
        type="http",
        auth="public",
        website=True,
        sitemap=True,
    )
    def careers_job(self, job, **kwargs):
        return self.job(job, **kwargs)

    @http.route(
        '/careers/apply/<model("hr.job"):job>',
        type="http",
        auth="public",
        website=True,
        sitemap=True,
    )
    def careers_apply(self, job, **kwargs):
        return self.jobs_apply(job, **kwargs)
