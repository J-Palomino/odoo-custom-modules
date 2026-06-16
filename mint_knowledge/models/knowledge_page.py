from odoo import api, fields, models


class MintKnowledgePage(models.Model):
    _name = "mint.knowledge.page"
    _description = "Knowledge Page"
    _order = "sequence, name"

    name = fields.Char(string="Title", required=True, translate=True)
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
