from odoo import fields, models


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    x_display_label_override = fields.Char(
        string='PTL Display Label Override',
        size=128,
        help='Override the bud-tender-facing display label for this product on '
             'PTL calendars and exports. Leave empty to use the canonical product '
             'name. Used for surgical naming fixes without renaming the underlying '
             'product (e.g., distinguishing weight variants in a budtender menu).',
    )

    def _get_ptl_display_label(self):
        """Return the budtender-facing label: override if set, else product name."""
        self.ensure_one()
        return self.x_display_label_override or self.name or ''
