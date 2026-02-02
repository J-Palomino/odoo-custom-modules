# -*- coding: utf-8 -*-
"""
Blog post model for MintDeals content.
"""
from odoo import api, fields, models


class MintBlog(models.Model):
    _name = "mint.blog"
    _description = "Blog Post"
    _order = "published_at desc, id desc"

    title = fields.Char(string="Title", required=True)
    slug = fields.Char(string="URL Slug", required=True, index=True)

    # Content
    excerpt = fields.Text(string="Excerpt")
    content = fields.Html(string="Content")
    read_time = fields.Integer(string="Read Time (minutes)")

    # Media
    image = fields.Binary(string="Feature Image")
    image_url = fields.Char(string="Feature Image URL")
    image_alt = fields.Char(string="Feature Image Alt Text")

    # Publishing
    is_published = fields.Boolean(string="Published", default=False)
    is_featured = fields.Boolean(string="Featured", default=False)
    published_at = fields.Datetime(string="Published At")

    # Author
    author_id = fields.Many2one('res.users', string="Author", default=lambda self: self.env.user)

    # Categorization
    category_id = fields.Many2one('mint.blog.category', string="Category")
    tag_ids = fields.Many2many('mint.blog.tag', string="Tags")

    # SEO
    meta_title = fields.Char(string="Meta Title")
    meta_description = fields.Text(string="Meta Description")

    _sql_constraints = [
        ('slug_unique', 'UNIQUE(slug)', 'Blog post slug must be unique!'),
    ]

    @api.model
    def get_published_posts(self, limit=None):
        """Return published blog posts."""
        domain = [('is_published', '=', True)]
        return self.search(domain, limit=limit)

    def get_tags_list(self):
        """Return tags as a list of names."""
        return [tag.name for tag in self.tag_ids]


class MintBlogCategory(models.Model):
    _name = "mint.blog.category"
    _description = "Blog Category"

    name = fields.Char(string="Name", required=True)
    slug = fields.Char(string="URL Slug")
    description = fields.Text(string="Description")


class MintBlogTag(models.Model):
    _name = "mint.blog.tag"
    _description = "Blog Tag"

    name = fields.Char(string="Name", required=True)
    slug = fields.Char(string="URL Slug")
    color = fields.Integer(string="Color Index")

    _sql_constraints = [
        ('name_unique', 'UNIQUE(name)', 'Tag name must be unique!'),
    ]
