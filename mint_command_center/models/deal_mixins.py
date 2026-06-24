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


# Dutchie day-of-week field names, Monday-first to match Python's date.weekday()
# (0=Mon … 6=Sun). NOTE: ALL-FALSE means "active EVERY day" in Dutchie — so a
# day-scoped deal must never resolve to all-False (see weekday_bools_from_days).
_WEEKDAY_BY_NUM = {0: 'monday', 1: 'tuesday', 2: 'wednesday', 3: 'thursday',
                   4: 'friday', 5: 'saturday', 6: 'sunday'}
DAY_FIELD_NAMES = tuple(_WEEKDAY_BY_NUM[i] for i in range(7))


def weekday_bools_from_days(days, span_from=None, span_to=None, market_id=None):
    """Reduce plotted PTL days to Monday..Sunday booleans.

    ``days`` is an iterable of ``(date, market_id)`` pairs. A weekday flag is
    True when at least one day falls on it, AFTER filtering to the inclusive
    span ``[span_from, span_to]`` (when given) and to ``market_id`` (when given,
    for per-market Dutchie pushes). Returns ``{monday: bool, … sunday: bool}``.

    Bounded by the discount's own validity span — NOT a fixed today+N horizon —
    so a future-dated deal resolves its real weekdays instead of collapsing to
    all-False (which Dutchie would read as "every day"). Callers must treat an
    all-False result for a day-scoped deal as a refuse-to-publish signal, never
    send it as-is.
    """
    bools = {name: False for name in DAY_FIELD_NAMES}
    for d, mid in days:
        if d is None:
            continue
        if span_from and d < span_from:
            continue
        if span_to and d > span_to:
            continue
        if market_id is not None and mid != market_id:
            continue
        bools[_WEEKDAY_BY_NUM[d.weekday()]] = True
    return bools


def dutchie_claim_decision(owner, path):
    """Pure cross-path publish mutex (#2). There are two paths that write a
    deal to Dutchie — the submission convert auto-publish and the PTL Publish
    button — and they use different ExternalIds/topologies, so both firing
    creates a DUPLICATE live discount. First live writer wins.

    ``owner`` is the deal's current ``dutchie_publish_owner``
    ('submission' | 'ptl' | falsy); ``path`` is the path requesting to publish.
    Returns ``(may_publish, owner_to_set)`` — ``owner_to_set`` is the value to
    persist (None = leave unchanged). Same-path re-publish is always allowed
    (idempotent update via that path's own id map); the OTHER path is blocked.
    """
    if owner and owner != path:
        return (False, None)
    return (True, None if owner else path)


def normalize_publish_mode(raw, unset_default='dry_run', valid=('off', 'dry_run', 'live')):
    """Normalize a ``dutchie.publish.mode`` value (#4 fail-closed).

    Unset/empty -> ``unset_default`` (preserves the historical default). A value
    that is SET but unrecognized -> 'off', never silently LIVE — path-1 used to
    treat anything that wasn't 'off'/'dry_run' as live (fail-open); this matches
    the PTL push path's fail-closed coercion.
    """
    if raw is None or str(raw).strip() == '':
        return unset_default
    m = str(raw).strip().lower()
    return m if m in valid else 'off'


def dutchie_deal_external_id(deal_id, lsp_id, span_index=None):
    """Unified deal+market-keyed Dutchie ExternalId (#2 Stage 2).

    Both publish paths derive the SAME id for a given (deal, market) so neither
    creates a parallel record. The LSP is in the key because one deal can publish
    to several markets — each market is a distinct Dutchie record, so they must
    NOT collide on one ExternalId. Single span -> ``lgm_deal_<id>_lsp<lsp>``;
    multi-span -> ``lgm_deal_<id>_lsp<lsp>_w<k>``. Replaces the divergent
    ``lgm_<submission_id>`` (path-1) and ``lgm_<discount_id>`` (path-2) schemes
    that let one deal become two Dutchie discounts.
    """
    if not lsp_id:
        raise ValueError(
            "dutchie_deal_external_id requires a non-zero lsp_id — refusing to "
            "build an ambiguous key (resolve the store's dutchie_lsp_id first).")
    base = "lgm_deal_%d_lsp%d" % (int(deal_id), int(lsp_id))
    return base if span_index is None else "%s_w%d" % (base, int(span_index))


def resolve_dutchie_discount_id(existing_local, readback):
    """Decide create-vs-update for a Dutchie upsert (#2 Stage 2).

    ``existing_local`` = the discount id from OUR records (registry/log), 0 if
    none. ``readback`` = what a live Dutchie lookup by ExternalId returned:
    a positive int (found that id), ``0`` (confirmed absent), or ``None`` (the
    read failed / is unknown).

    Returns ``(id_to_use, action)`` where action is 'update' | 'create' |
    'abort'. Rules:
      * Dutchie read is authoritative: a found id -> update it.
      * Confirmed-absent -> create fresh (Id=0).
      * Unknown (read failed) -> NEVER blind-create (the empty-map / sub-395
        lesson): update our known local id if we have one, else abort so the
        caller skips rather than risking a duplicate.
    """
    if isinstance(readback, bool):
        readback = None  # guard: bool is an int subclass; treat as unknown
    if isinstance(readback, int) and readback > 0:
        return (readback, 'update')
    if readback == 0:
        return (0, 'create')
    # readback is None -> unknown / read failed
    if existing_local and int(existing_local) > 0:
        return (int(existing_local), 'update')
    return (0, 'abort')


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
