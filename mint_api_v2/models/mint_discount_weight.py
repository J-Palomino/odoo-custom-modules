# -*- coding: utf-8 -*-
import logging

from psycopg2 import IntegrityError

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


# Weight values are quantized to this many decimals before storage / comparison
# so JSON round-trips and 1-ULP float drift on values like 3.5 don't produce
# duplicate rows or bypass the UNIQUE(value) guard.
_WEIGHT_QUANTIZE_DECIMALS = 4


def _quantize(value):
    """Return `float(value)` rounded to the canonical decimal places, or None."""
    if value is None or value == "" or value is False:
        return None
    try:
        return round(float(value), _WEIGHT_QUANTIZE_DECIMALS)
    except (TypeError, ValueError):
        return None


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
        string="Weight (grams)", required=True, digits=(10, _WEIGHT_QUANTIZE_DECIMALS),
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

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if "value" in vals:
                q = _quantize(vals["value"])
                if q is None:
                    # Unparseable — drop the key so Odoo surfaces the missing
                    # required field rather than silently storing a junk value.
                    del vals["value"]
                else:
                    vals["value"] = q
        return super().create(vals_list)

    def write(self, vals):
        if "value" in vals:
            q = _quantize(vals["value"])
            if q is None:
                del vals["value"]
            else:
                vals["value"] = q
        return super().write(vals)

    @api.model
    def get_or_create(self, value):
        """Return an existing record for `value`, or create one.

        Race-safe: if a concurrent transaction created the same value between
        our search-miss and create, the UNIQUE(value) constraint fires an
        IntegrityError inside a savepoint, we roll back and re-search.
        """
        v = _quantize(value)
        if v is None:
            return self.browse()
        existing = self.search([("value", "=", v)], limit=1)
        if existing:
            return existing
        try:
            with self.env.cr.savepoint():
                return self.create({"value": v})
        except IntegrityError:
            # Another transaction committed the same value between search and
            # create. Re-search — it must exist now.
            existing = self.search([("value", "=", v)], limit=1)
            if existing:
                return existing
            # Re-search empty is unexpected (DB constraint fired but row isn't
            # visible). Log and return empty so the caller's downstream
            # "unresolved" counter bumps instead of silently over-matching.
            _logger.warning(
                "[mint.discount.weight] get_or_create(%s): IntegrityError on "
                "create but re-search returned empty — weight filter will drop "
                "for this discount cycle; callers should treat as unresolved.",
                v,
            )
            return self.browse()
