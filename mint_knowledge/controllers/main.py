from odoo import http
from odoo.http import request
from werkzeug.exceptions import Forbidden, NotFound

MANAGER_GROUP = "mint_knowledge.group_mint_knowledge_manager"


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

    def _edit_action_id(self):
        # Resolve the backend Pages action id (avoids hardcoding) for the
        # manager-only Edit button.
        action = request.env.ref(
            "mint_knowledge.action_mint_knowledge_page", raise_if_not_found=False
        )
        return action.id if action else False

    @http.route("/it-knowledge", type="http", auth="user", website=True, sitemap=False)
    def kb_index(self, search=None, **kw):
        self._require_employee()
        env = request.env
        is_manager = env.user.has_group(MANAGER_GROUP)
        values = {
            "search": search or "",
            "is_manager": is_manager,
            "edit_action_id": self._edit_action_id(),
        }
        if search:
            values["results"] = (
                env["mint.knowledge.page"]
                .sudo()
                .search(
                    ["|", ("name", "ilike", search), ("content", "ilike", search)],
                    order="name",
                )
            )
        else:
            values["categories"] = (
                env["mint.knowledge.category"]
                .sudo()
                .search([("parent_id", "=", False)], order="sequence, complete_name")
            )
        return request.render("mint_knowledge.kb_index", values)

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
        return request.render(
            "mint_knowledge.kb_page",
            {
                "page": page,
                "is_manager": request.env.user.has_group(MANAGER_GROUP),
                "edit_action_id": self._edit_action_id(),
            },
        )
