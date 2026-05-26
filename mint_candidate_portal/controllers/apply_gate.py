from werkzeug.urls import url_encode

from odoo import http
from odoo.http import request

from odoo.addons.website_hr_recruitment.controllers.main import WebsiteHrRecruitment


class CandidateApplyGate(WebsiteHrRecruitment):
    """Gate /jobs/apply/<job> behind a logged-in user.

    Anonymous visitors are redirected to /web/signup with the apply URL
    preserved as the post-signup redirect. After signup, Odoo's auth_signup
    flow lands them back on the apply form, now authenticated, with form
    fields auto-filled from their res.partner.

    The same gate covers /careers/apply/<job> when mint_career is installed —
    mint_career.MintCareer.careers_apply calls self.jobs_apply, so the
    override here runs for both URLs.
    """

    def _gate_or_none(self):
        if request.env.user._is_public():
            return request.redirect(
                "/web/signup?" + url_encode({"redirect": request.httprequest.full_path})
            )
        return None

    @http.route(
        '/jobs/apply/<model("hr.job"):job>',
        type="http",
        auth="public",
        website=True,
        sitemap=True,
    )
    def jobs_apply(self, job, **kwargs):
        gate = self._gate_or_none()
        if gate is not None:
            return gate
        return super().jobs_apply(job, **kwargs)
