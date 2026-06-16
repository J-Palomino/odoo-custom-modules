from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class MintKnowledgeCategory(models.Model):
    _name = "mint.knowledge.category"
    _description = "Knowledge Category"
    _parent_store = True
    _parent_name = "parent_id"
    _order = "complete_name"

    name = fields.Char(required=True, translate=True)
    complete_name = fields.Char(
        compute="_compute_complete_name", recursive=True, store=True
    )
    parent_id = fields.Many2one(
        "mint.knowledge.category",
        string="Parent Category",
        ondelete="cascade",
        index=True,
    )
    parent_path = fields.Char(index=True, unaccent=False)
    child_ids = fields.One2many(
        "mint.knowledge.category", "parent_id", string="Subcategories"
    )
    sequence = fields.Integer(default=10)
    description = fields.Text()
    page_ids = fields.One2many(
        "mint.knowledge.page", "category_id", string="Pages"
    )
    page_count = fields.Integer(compute="_compute_page_count")

    @api.depends("name", "parent_id.complete_name")
    def _compute_complete_name(self):
        for category in self:
            if category.parent_id:
                category.complete_name = (
                    f"{category.parent_id.complete_name} / {category.name}"
                )
            else:
                category.complete_name = category.name

    def _compute_page_count(self):
        for category in self:
            category.page_count = len(category.page_ids)

    @api.constrains("parent_id")
    def _check_category_recursion(self):
        if self._has_cycle():
            raise ValidationError(_("You cannot create recursive categories."))

    def action_view_pages(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": self.name,
            "res_model": "mint.knowledge.page",
            "view_mode": "list,form",
            "domain": [("category_id", "=", self.id)],
            "context": {"default_category_id": self.id},
        }
