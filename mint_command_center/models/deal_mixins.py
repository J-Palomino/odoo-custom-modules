from odoo import fields, models

# Single source of truth for selections that were previously copy-pasted across
# the deal models (mint.ptl.deal, mint.deal.submission, mint.hotbox.deal,
# mint.brand.calendar.entry). Import and reference these instead of re-listing
# the tuples so the enums can't drift apart.
DISCOUNT_TYPE_SELECTION = [
    ('percent', 'Percentage Off'),
    ('fixed', 'Fixed Amount Off'),
    ('bogo', 'BOGO'),
    ('bundle', 'Bundle Deal'),
    ('price', 'Set Price'),
    ('points_multiplier', 'Loyalty Points Multiplier'),
    ('clearance', 'Clearance (Near Expiry)'),
]

WEIGHT_UNIT_SELECTION = [
    ('g', 'g'),
    ('mg', 'mg'),
    ('oz', 'oz'),
    ('ct', 'ct'),
]


class MintDiscountCoreMixin(models.AbstractModel):
    """Discount type/value + MSRP, shared by every Mint deal model.

    Field names are preserved verbatim so the existing DB columns, views,
    security rules and RPC callers are untouched. Models that need a quirk
    (extra help text, tracking, a different label) re-declare just that
    attribute — Odoo merges field definitions incrementally across _inherit."""

    _name = 'mint.discount.core.mixin'
    _description = 'Mixin: shared discount type/value/MSRP fields'

    discount_type = fields.Selection(
        selection=DISCOUNT_TYPE_SELECTION,
        string='Discount Type',
    )
    discount_value = fields.Float(string='Discount Value')
    original_price = fields.Float(string='Original / MSRP Price')


class MintVendorFundingMixin(models.AbstractModel):
    """Vendor-funding amount/percent + currency, shared by the deal models that
    carry vendor co-op terms (ptl deal, deal submission, brand calendar entry)."""

    _name = 'mint.vendor.funding.mixin'
    _description = 'Mixin: shared vendor-funding fields'

    vendor_funding_amount = fields.Monetary(
        string='Vendor Funding Amount',
        currency_field='currency_id',
        tracking=True,
    )
    vendor_funding_percent = fields.Float(string='Vendor Funding %', tracking=True)
    currency_id = fields.Many2one(
        'res.currency',
        string='Currency',
        default=lambda self: self.env.company.currency_id,
    )


class MintWeightParsedMixin(models.AbstractModel):
    """Parsed weight/unit, auto-derived from a model-specific title via
    _parse_weight. Used by ptl deal + brand calendar entry.

    The two consumers parse from different source fields, so the compute's
    @api.depends differs per model. We keep the field definitions here (the
    bulk of the duplication) and let each concrete model override the tiny
    _weight_source() hook plus a thin @api.depends wrapper around super()."""

    _name = 'mint.weight.parsed.mixin'
    _description = 'Mixin: shared parsed weight/unit fields'

    weight_value = fields.Float(
        string='Weight',
        compute='_compute_weight',
        store=True,
        readonly=False,
        tracking=True,
        help='Numeric weight/count parsed from the deal name (e.g. "Aeriz 1g AIO" → 1.0). '
             'Manually editable; clear the name/sales_details to auto-recompute.',
    )
    weight_unit = fields.Selection(
        selection=WEIGHT_UNIT_SELECTION,
        string='Unit',
        compute='_compute_weight',
        store=True,
        readonly=False,
        tracking=True,
    )

    def _weight_source(self):
        """Return the (title, detail) strings to parse weight from.
        Concrete models override with their own source fields."""
        self.ensure_one()
        return (False, False)

    def _compute_weight(self):
        # No @api.depends here on purpose: the source fields differ per model,
        # so each concrete model overrides this with a thin @api.depends wrapper
        # that calls super(). _parse_weight is imported lazily to avoid any
        # models/__init__ load-order coupling with brand_calendar.
        from .brand_calendar import _parse_weight
        for rec in self:
            value, unit = _parse_weight(*rec._weight_source())
            rec.weight_value = value
            rec.weight_unit = unit or False
