# -*- coding: utf-8 -*-
"""
Key-value config model for frontend configuration.
"""
from odoo import fields, models


class MintConfig(models.Model):
    _name = "mint.config"
    _description = "Frontend Configuration"
    _order = "key asc"
    _rec_name = "key"

    key = fields.Char(string="Key", required=True, index=True)
    value = fields.Text(string="Value (JSON)")
    description = fields.Text(string="Description")
    is_active = fields.Boolean(string="Active", default=True)

    _key_unique = models.Constraint(
        'unique(key)',
        'Config key must be unique.',
    )
