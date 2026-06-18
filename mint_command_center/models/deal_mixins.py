from odoo import api, fields, models

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

# BOGO structured variants (#93677 Cluster C / tickets #93673-#93676). The
# variant dropdown is UI sugar over the canonical buy/get/pct triple — picking
# a preset fills the quantities; 'custom' leaves them editable.
BOGO_VARIANT_SELECTION = [
    ('b1g1', 'B1G1 — Buy 1 Get 1'),
    ('b2g1', 'B2G1 — Buy 2 Get 1'),
    ('b3g1', 'B3G1 — Buy 3 Get 1'),
    ('custom', 'Custom quantities'),
]

BOGO_VARIANT_QTYS = {
    'b1g1': (1, 1),
    'b2g1': (2, 1),
    'b3g1': (3, 1),
}


def format_bogo_text(buy_qty, get_qty, get_pct):
    """Customer-facing text for a structured BOGO spec.

    (1, 1, 1.0) -> 'B1G1 Free'; (1, 1, 0.5) -> 'B1G1 50% Off';
    (2, 2, 1.0) -> 'B2G2 Free'. Falls back to '' when the triple is unset
    so callers keep their legacy 'BOGO' literal for un-migrated rows.
    """
    if not buy_qty or not get_qty:
        return ''
    pct = get_pct or 0.0
    if pct > 1:                      # tolerate 50 meaning 50%
        pct = pct / 100.0
    label = f"B{int(buy_qty)}G{int(get_qty)}"
    if pct >= 1.0 or pct <= 0:
        return f"{label} Free"
    return f"{label} {pct * 100:g}% Off"


def format_bundle_tiers_text(tiers):
    """Render bundle tiers literally, joined with ' or ' (#93677): tiers
    [(2, 18.0), (3, 25.0)] -> '2 for $18 or 3 for $25'. No per-unit math."""
    parts = []
    for qty, price in tiers:
        if not qty or not price:
            continue
        p = f"${price:.0f}" if price == int(price) else f"${price:.2f}"
        parts.append(f"{int(qty)} for {p}")
    return ' or '.join(parts)


# Dutchie discount restriction types. One IsExclusion flag + id list per type;
# Dutchie applies them as an INTERSECTION (a product must satisfy every
# populated type). Keep this ordering stable — it mirrors the Backoffice shape.
DUTCHIE_RESTRICTION_TYPES = ('Strain', 'Weight', 'Category', 'Tag',
                             'InventoryTag', 'Tier', 'Brand', 'Vendor', 'Product')


def build_dutchie_restrictions(brand_ids, exc_brand_ids, prod_inc, prod_exc, cat_ids):
    """Assemble the Dutchie ``Reward.Restrictions`` dict + warnings from already
    resolved id lists. Returns ``(restrictions, warnings)``.

    Dutchie AND's restriction types together, so a Product INCLUDE layered on
    top of a Brand/Category include can only SHRINK eligibility — never what we
    intend (deal sub 395 "IO Extracts — 2 for $35" published with
    Brand+Category+239 products applied to 48 products instead of the 233 that
    Brand+Category alone cover). Send the Product include ONLY when it is the
    sole scoping signal; otherwise drop it (warn) and keep product EXCLUDES.
    """
    restrictions = {k: {'IsExclusion': False, 'RestrictionIds': []}
                    for k in DUTCHIE_RESTRICTION_TYPES}
    warnings = []
    if brand_ids:
        restrictions['Brand'] = {'IsExclusion': False, 'RestrictionIds': brand_ids}
    elif exc_brand_ids:
        restrictions['Brand'] = {'IsExclusion': True, 'RestrictionIds': exc_brand_ids}
    if prod_inc and not brand_ids and not cat_ids:
        restrictions['Product'] = {'IsExclusion': False, 'RestrictionIds': prod_inc}
        if prod_exc:
            warnings.append("product exclusions dropped (Product slot used by includes)")
    elif prod_exc:
        restrictions['Product'] = {'IsExclusion': True, 'RestrictionIds': prod_exc}
    elif prod_inc:
        warnings.append(
            "%d explicit product include(s) dropped — Brand/Category already "
            "scope this deal; an extra Product include would only shrink "
            "eligibility (Dutchie intersects restriction types)" % len(prod_inc))
    if cat_ids:
        restrictions['Category'] = {'IsExclusion': False, 'RestrictionIds': cat_ids}
    return restrictions, warnings


WEIGHT_UNIT_SELECTION = [
    ('g', 'g'),
    ('mg', 'mg'),
    ('oz', 'oz'),
    ('ct', 'ct'),
]


def coerce_dutchie_ids(records, field):
    """Map a recordset's Char Dutchie cross-reference IDs to a de-duplicated,
    order-preserving list of ints.

    Single source of truth for building Reward.Restriction RestrictionIds on
    both push paths (mint.ptl.day publish -> webhook, and the Dutchie discount
    push). Records with an empty / non-numeric Dutchie ID are dropped — the
    downstream resolver indexes by Dutchie ID and would miss them anyway.

    De-dup is required: the category widening (MASTER_CATEGORY_PATTERNS) expands
    several product.category records that share one dutchie_category_id, which
    otherwise emits repeated RestrictionIds. A restriction is a set, not a list.
    """
    out = []
    seen = set()
    for r in records:
        val = getattr(r, field, None)
        if not val:
            continue
        try:
            i = int(str(val).strip())
        except (TypeError, ValueError):
            continue
        if i in seen:
            continue
        seen.add(i)
        out.append(i)
    return out


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


class MintBogoSpecMixin(models.AbstractModel):
    """Structured BOGO spec (#93677 Cluster C) — inherited ONLY by the two
    models that render/sync it (mint.ptl.deal, mint.deal.submission), not by
    the core discount mixin: hotbox + brand calendar share discount_type but
    have no views or sync for the triple, and broadcasting inert columns
    there invites invisible state.

    Canonical storage is the buy/get/pct triple; bogo_variant is the
    dropdown operators/vendors actually pick. Visible only when
    discount_type == 'bogo' (view-level), inert for every other type."""

    _name = 'mint.bogo.spec.mixin'
    _description = 'Mixin: structured BOGO buy/get/pct spec'

    bogo_variant = fields.Selection(
        selection=BOGO_VARIANT_SELECTION,
        string='BOGO Variant',
    )
    bogo_buy_qty = fields.Integer(
        string='Buy Qty',
        default=0,
        help='Units the customer must buy at full price.',
    )
    bogo_get_qty = fields.Integer(
        string='Get Qty',
        default=0,
        help='Units discounted once the buy quantity is met.',
    )
    bogo_get_pct = fields.Float(
        string='Get Discount',
        default=1.0,
        help='0–1 fractional discount on the get quantity; 1.0 = free, '
             '0.5 = 50% off the second item.',
    )

    @api.onchange('bogo_variant')
    def _onchange_bogo_variant(self):
        for rec in self:
            qtys = BOGO_VARIANT_QTYS.get(rec.bogo_variant)
            if qtys:
                rec.bogo_buy_qty, rec.bogo_get_qty = qtys

    @api.onchange('discount_type')
    def _onchange_discount_type_bogo_default(self):
        for rec in self:
            if rec.discount_type == 'bogo' and not rec.bogo_variant:
                rec.bogo_variant = 'b1g1'
                rec.bogo_buy_qty, rec.bogo_get_qty = BOGO_VARIANT_QTYS['b1g1']

    def _bogo_sales_text(self):
        self.ensure_one()
        return format_bogo_text(
            self.bogo_buy_qty, self.bogo_get_qty, self.bogo_get_pct)


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
