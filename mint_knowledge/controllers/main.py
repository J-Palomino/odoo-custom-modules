from odoo import http
from odoo.http import request
from werkzeug.exceptions import Forbidden, NotFound

MANAGER_GROUP = "mint_knowledge.group_mint_knowledge_manager"


class KnowledgeController(http.Controller):
    """Logged-in staff portal for the company Knowledge Base.

    auth='user' (must be signed in); access is further restricted to internal
    employees (share=False). Records are read with sudo() so any employee can
    consume the KB without needing the backend Knowledge ACL group.

    Structure: top-level categories are "sections" (e.g. IT, HR, Marketing);
    each section has its own page listing its articles and sub-sections.
    """

    def _require_employee(self):
        user = request.env.user
        if not user or user._is_public() or user.share:
            raise Forbidden()

    def _is_manager(self):
        return request.env.user.has_group(MANAGER_GROUP)

    def _action_id(self, xmlid):
        action = request.env.ref(xmlid, raise_if_not_found=False)
        return action.id if action else False

    def _common(self):
        return {
            "is_manager": self._is_manager(),
            "edit_page_action_id": self._action_id(
                "mint_knowledge.action_mint_knowledge_page"
            ),
            "edit_cat_action_id": self._action_id(
                "mint_knowledge.action_mint_knowledge_category"
            ),
        }

    @http.route("/knowledge", type="http", auth="user", website=True, sitemap=False)
    def kb_index(self, search=None, **kw):
        self._require_employee()
        env = request.env
        values = dict(self._common(), search=search or "")
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
            values["sections"] = (
                env["mint.knowledge.category"]
                .sudo()
                .search([("parent_id", "=", False)], order="sequence, complete_name")
            )
        return request.render("mint_knowledge.kb_index", values)

    @http.route(
        "/knowledge/section/<int:category_id>",
        type="http",
        auth="user",
        website=True,
        sitemap=False,
    )
    def kb_section(self, category_id, **kw):
        self._require_employee()
        section = request.env["mint.knowledge.category"].sudo().browse(category_id)
        if not section.exists():
            raise NotFound()
        return request.render(
            "mint_knowledge.kb_section", dict(self._common(), section=section)
        )

    def _backlinks(self, page):
        """Other pages whose content links to this page (by id or slug)."""
        Page = request.env["mint.knowledge.page"].sudo()
        terms = ['/knowledge/page/%s"' % page.id]
        if page.slug:
            terms.append('/knowledge/p/%s"' % page.slug)
        content_dom = [("content", "ilike", t) for t in terms]
        if len(content_dom) > 1:
            content_dom = ["|"] * (len(content_dom) - 1) + content_dom
        return Page.search(["&", ("id", "!=", page.id)] + content_dom, order="name")

    def _render_page(self, page):
        return request.render(
            "mint_knowledge.kb_page",
            dict(self._common(), page=page, backlinks=self._backlinks(page)),
        )

    @http.route(
        "/knowledge/page/<int:page_id>",
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
        return self._render_page(page)

    @http.route(
        "/knowledge/p/<string:slug>",
        type="http",
        auth="user",
        website=True,
        sitemap=False,
    )
    def kb_page_slug(self, slug, **kw):
        self._require_employee()
        page = (
            request.env["mint.knowledge.page"].sudo().search([("slug", "=", slug)], limit=1)
        )
        if not page:
            raise NotFound()
        return self._render_page(page)

    # ── Backward-compat: old IT-scoped URLs ──────────────────────────────
    @http.route("/it-knowledge", type="http", auth="user", website=True, sitemap=False)
    def legacy_index(self, **kw):
        return request.redirect("/knowledge", code=301)

    @http.route(
        "/it-knowledge/page/<int:page_id>",
        type="http",
        auth="user",
        website=True,
        sitemap=False,
    )
    def legacy_page(self, page_id, **kw):
        return request.redirect("/knowledge/page/%s" % page_id, code=301)
