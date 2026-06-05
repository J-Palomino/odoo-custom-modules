"""Mixin for models that HOST a one2many of discount options.

mint.ptl.deal and mint.deal.submission both:
  1. carry an option_ids One2many to a discount.option child model,
  2. expose discount_type / discount_value as computed-stored mirrors
     of the first option (sorted by sequence, id),
  3. provide an inverse that upserts/unlinks the first option when the
     mirror is written directly (legacy callers, vendor controller).

The mirror compute + inverse + helpers were ~45 LOC duplicated on each
host model with only the o2m field name and child model name differing.
This mixin owns them; concrete hosts declare:
  - _option_model        — Odoo model name of the option, e.g. 'mint.ptl.deal.option'
  - _option_fk_field     — option's M2O back to self, e.g. 'ptl_deal_id'
  - the option_ids One2many field (declared on the concrete model
    because Odoo requires the comodel name to be a literal at field
    declaration time, not a class attribute reference)
  - the discount_type / discount_value fields with
    compute='_compute_primary_option_fields',
    inverse='_inverse_primary_option_fields',
    store=True, readonly=False.
"""
from odoo import api, fields, models


class DiscountOptionHostMixin(models.AbstractModel):
    _name = 'mint.discount.option.host.mixin'
    _description = 'Hosts a one2many of discount.option records and mirrors the first one'

    # Concrete hosts override these.
    _option_model = None
    _option_fk_field = None

    def _primary_option(self):
        """First option by (sequence, id). In-cache o2m isn't guaranteed to
        be re-sorted after a same-transaction sequence write, so sort
        explicitly. Shared by the compute, the inverse, and the post-migrate
        backfills.
        """
        self.ensure_one()
        return self.option_ids.sorted(lambda o: (o.sequence, o.id))[:1]

    def _primary_option_vals(self, discount_type, discount_value):
        """Single source of truth for the create-vals dict of a primary
        option row. Used by the inverse and the post-migrate backfill so
        they can't drift when the option model gains required fields.
        """
        return {
            self._option_fk_field: self.id,
            'sequence': 10,
            'discount_type': discount_type,
            'discount_value': discount_value or 0.0,
        }

    @api.depends('option_ids', 'option_ids.discount_type',
                 'option_ids.discount_value', 'option_ids.sequence')
    def _compute_primary_option_fields(self):
        for rec in self:
            primary = rec._primary_option()
            rec.discount_type = primary.discount_type if primary else False
            rec.discount_value = primary.discount_value if primary else 0.0

    def _inverse_primary_option_fields(self):
        """Legacy write-path: writing discount_type/value on the host
        upserts the first option; clearing discount_type unlinks the
        primary so the mirror doesn't snap back on next recompute.

        rec.discount_value is a Float (always 0.0 or a real value, never
        False) so the cache holds the previously-mirrored value when the
        caller only wrote discount_type — we don't clobber the option's
        value with 0.0.
        """
        Option = self.env[self._option_model]
        for rec in self:
            primary = rec._primary_option()
            if not rec.discount_type:
                if primary:
                    primary.unlink()
                continue
            if primary:
                primary.write({
                    'discount_type': rec.discount_type,
                    'discount_value': rec.discount_value,
                })
            else:
                Option.create(rec._primary_option_vals(
                    rec.discount_type, rec.discount_value,
                ))
