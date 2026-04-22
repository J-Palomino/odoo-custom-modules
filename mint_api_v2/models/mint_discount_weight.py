# -*- coding: utf-8 -*-
from odoo import api, fields, models


class MintDiscountWeight(models.Model):
    """A weight value (grams) used to scope mint.discount records.

    Mirrors Dutchie's Reward.Restrictions.Weight.RestrictionIds — typical values
    are 0.5, 0.7, 1.0, 2.0, 3.5, 7.0, 14.0, 28.0. Records are auto-upserted by
    the Dutchie→Odoo discount sync; operators can also manage them in the UI.
    """

    _name = "mint.discount.weight"
    _description = "Dutchie Weight Restriction Value"
    _order = "value"
    _rec_name = "name"

    value = fields.Float(
        string="Weight (grams)", required=True, digits=(10, 4),
        help="Weight value in grams. Matches product.template.x_weight_grams.",
    )
    name = fields.Char(compute="_compute_name", store=True, index=True)
    active = fields.Boolean(default=True)

    _sql_constraints = [
        ("value_unique", "UNIQUE(value)", "Weight value must be unique."),
    ]

    @api.depends("value")
    def _compute_name(self):
        for rec in self:
            if rec.value is None:
                rec.name = ""
            elif rec.value == int(rec.value):
                rec.name = "%dg" % int(rec.value)
            else:
                rec.name = ("%g" % rec.value) + "g"

    @api.model
    def get_or_create(self, value):
        """Return an existing record for `value`, or create one. Used by sync."""
        if value is None or value == "" or value is False:
            return self.browse()
        try:
            v = float(value)
        except (TypeError, ValueError):
            return self.browse()
        existing = self.search([("value", "=", v)], limit=1)
        return existing or self.create({"value": v})
