from odoo import http
from odoo.http import request
from werkzeug.exceptions import Forbidden, NotFound


class ITKnowledgeController(http.Controller):
    """Logged-in staff portal for the Knowledge Base.

    auth='user' (must be signed in); access is further restricted to internal
    employees (share=False). Records are read with sudo() so any employee can
    consume the KB without needing the backend Knowledge ACL group.
    """

    def _require_employee(self):
        user = request.env.user
        if not user or user._is_public() or user.share:
            raise Forbidden()

    @http.route("/it-knowledge", type="http", auth="user", website=True, sitemap=False)
    def kb_index(self, **kw):
        self._require_employee()
        categories = (
            request.env["mint.knowledge.category"]
            .sudo()
            .search([("parent_id", "=", False)], order="sequence, complete_name")
        )
        return request.render(
            "mint_knowledge.kb_index", {"categories": categories}
        )

    @http.route(
        "/it-knowledge/page/<int:page_id>",
        type="http",
        auth="user",
        website=True,
        sitemap=False,
    )
    def kb_page(self, page_id, **kw):
        self._require_employee()
        page = request.env["mint.knowledge.page"].sudo().browse(page_id)
        if not page.exists():
            raise NotFound()
        return request.render("mint_knowledge.kb_page", {"page": page})
