"""Shared base for option models that carry a single discount spec.

mint.ptl.deal.option and mint.deal.submission.option were 95% identical
copy-paste pre-refactor — same Selection enum, same _compute_display_text
formatter (49 lines), differing only in which parent FK they hang off.
This mixin owns both so the format never drifts between vendor-facing
submission and internal ptl.deal.

Concrete option models override:
- _name, _description, _order
- _parent_field — name of the Many2one back to the host (e.g. 'ptl_deal_id')
- _parent_msrp_path — dotted path the display formatter reads to pick up
  the host's original_price (e.g. 'ptl_deal_id.original_price')

The concrete model must also declare its own parent FK + add the
@api.depends on the parent's original_price field name on
_compute_display_text (re-decorated via inheritance).
"""
from odoo import api, fields, models


DISCOUNT_TYPE_SELECTION = [
    ('percent', 'Percentage Off'),
    ('fixed', 'Fixed Amount Off'),
    ('bogo', 'BOGO'),
    ('bundle', 'Bundle Deal'),
    ('price', 'Set Price'),
    ('points_multiplier', 'Loyalty Points Multiplier'),
    ('clearance', 'Clearance (Near Expiry)'),
]


def format_discount_display(dtype, value, msrp, sales_details):
    """Pure helper — produces the customer-facing pricing display string.

    Identical formatter to the one previously inlined on both option
    models. Called from _compute_display_text; tested implicitly via the
    multi-option integration tests.
    """
    if sales_details:
        return sales_details

    val = value or 0
    msrp = msrp or 0

    if dtype == 'percent' and val:
        pct = val if val > 1 else val * 100
        if msrp:
            sale = msrp * (1 - pct / 100)
            return f"~~${msrp:.0f}~~ ${sale:.2f} | {pct:.0f}% Off"
        return f"{pct:.0f}% Off"
    if dtype == 'fixed' and val:
        if msrp:
            sale = msrp - val
            return f"~~${msrp:.0f}~~ ${sale:.2f} | ${val:.0f} Off"
        return f"${val:.0f} Off"
    if dtype == 'price' and val:
        if msrp:
            return f"~~${msrp:.0f}~~ ${val:.2f}"
        return f"${val:.2f}"
    if dtype == 'bogo':
        return f"Starting @ ${msrp:.0f} | BOGO" if msrp else "Buy One Get One"
    if dtype == 'bundle' and val:
        return f"${msrp:.0f} Value! Only ${val:.2f}" if msrp else f"${val:.2f} Bundle"
    if dtype == 'points_multiplier' and val:
        mult = val if val >= 1 else (1 / val if val else 0)
        return f"{int(mult)}x Points" if mult == int(mult) else f"{mult:.1f}x Points"
    if dtype == 'clearance':
        pct = (val if val > 1 else val * 100) if val else 0
        if msrp and pct:
            sale = msrp * (1 - pct / 100)
            return f"Clearance: ~~${msrp:.0f}~~ ${sale:.2f} | {pct:.0f}% Off"
        if pct:
            return f"Clearance: {pct:.0f}% Off"
        return "Clearance"
    return ''


class DiscountOptionMixin(models.AbstractModel):
    """Common scaffold for single-discount-spec child models.

    Owns: sequence, discount_type, discount_value, sales_details,
    display_text + the formatter compute. Concrete models add the parent
    Many2one and override _parent_msrp_path.
    """
    _name = 'mint.discount.option.mixin'
    _description = 'Discount option mixin (shared shape for option models)'

    # Concrete models override this with the dotted path the formatter
    # reads to resolve the parent's MSRP (e.g. 'ptl_deal_id.original_price').
    _parent_msrp_path = None

    sequence = fields.Integer(string='Sequence', default=10)

    discount_type = fields.Selection(
        selection=DISCOUNT_TYPE_SELECTION,
        string='Discount Type',
        required=True,
    )
    discount_value = fields.Float(
        string='Discount Value',
        help='For points_multiplier, this is the points multiplier (e.g. 2.0 = 2x points).',
    )
    sales_details = fields.Text(
        string='Sales Details',
        help='Optional per-option override for the customer-facing pricing text.',
    )

    display_text = fields.Char(
        string='Display Text',
        compute='_compute_display_text',
        store=True,
        help='Auto-formatted pricing display for this option.',
    )

    def _resolve_parent_msrp(self):
        """Walk the dotted _parent_msrp_path off self to return a float.

        Subclasses set _parent_msrp_path (e.g. 'ptl_deal_id.original_price');
        this resolves it without each subclass re-implementing the walk.
        """
        self.ensure_one()
        if not self._parent_msrp_path:
            return 0.0
        node = self
        for part in self._parent_msrp_path.split('.'):
            node = getattr(node, part, None)
            if node is None or node is False:
                return 0.0
        return float(node or 0)

    @api.depends('discount_type', 'discount_value', 'sales_details')
    def _compute_display_text(self):
        # Subclasses MUST re-decorate this with @api.depends that includes
        # their parent's original_price path so the mirror refreshes when
        # the parent's MSRP changes. The base depends list only covers the
        # locally-owned fields.
        for rec in self:
            rec.display_text = format_discount_display(
                rec.discount_type,
                rec.discount_value,
                rec._resolve_parent_msrp(),
                rec.sales_details,
            )
