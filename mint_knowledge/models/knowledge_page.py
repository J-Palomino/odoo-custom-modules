import re

from odoo import api, fields, models


def _slugify(value):
    value = (value or "").lower().strip()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "page"


class MintKnowledgePage(models.Model):
    _name = "mint.knowledge.page"
    _description = "Knowledge Page"
    _order = "sequence, name"

    name = fields.Char(string="Title", required=True, translate=True)
    slug = fields.Char(
        index=True,
        copy=False,
        help="URL-friendly identifier used in /knowledge/p/<slug>. "
        "Auto-generated from the title; safe to edit, but changing it "
        "breaks existing links to this page.",
    )
    public_url = fields.Char(
        string="Public URL", compute="_compute_public_url",
        help="Copy this to link to the page from another article.",
    )
    category_id = fields.Many2one(
        "mint.knowledge.category",
        string="Category",
        required=True,
        ondelete="restrict",
        index=True,
    )
    content = fields.Html(string="Content", sanitize=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    author_id = fields.Many2one(
        "res.users",
        string="Author",
        default=lambda self: self.env.user,
        readonly=True,
    )

    _sql_constraints = [
        ("slug_uniq", "unique(slug)", "The page slug must be unique."),
    ]

    @api.depends("slug")
    def _compute_public_url(self):
        base = self.env["ir.config_parameter"].sudo().get_param("web.base.url") or ""
        for page in self:
            if page.slug:
                page.public_url = f"{base}/knowledge/p/{page.slug}"
            elif page.id:
                page.public_url = f"{base}/knowledge/page/{page.id}"
            else:
                page.public_url = False

    def _unique_slug(self, base_slug, exclude_id=None):
        candidate = base_slug
        suffix = 2
        while True:
            domain = [("slug", "=", candidate)]
            if exclude_id:
                domain.append(("id", "!=", exclude_id))
            if not self.sudo().search_count(domain):
                return candidate
            candidate = f"{base_slug}-{suffix}"
            suffix += 1

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get("slug"):
                vals["slug"] = self._unique_slug(_slugify(vals.get("name")))
        return super().create(vals_list)

    def write(self, vals):
        res = super().write(vals)
        # Backfill a slug only when missing; never auto-rewrite an existing
        # slug (that would silently break inbound links).
        for page in self:
            if not page.slug:
                super(MintKnowledgePage, page).write(
                    {"slug": page._unique_slug(_slugify(page.name), exclude_id=page.id)}
                )
        return res
