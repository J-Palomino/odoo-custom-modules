import logging
from datetime import timedelta

from markupsafe import Markup

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError

from .brand_calendar import _brand_lookup_key, _parse_brand_name
from .deal_mixins import format_bundle_tiers_text, dutchie_claim_decision

_logger = logging.getLogger(__name__)


# Master master-category buckets used in the PTL Calendar sheet,
# mapped to the product.category name fragments that fall under each.
# Used by _resolve_master_categories to widen the category search so a
# "Flower" master bucket includes "Prepack Flower", "Bulk Flower", etc.
# Adjust here if Odoo's product.category taxonomy changes.
MASTER_CATEGORY_PATTERNS = {
    'Flower': [
        'Flower', 'Bulk Flower', 'Prepack Flower', 'Shake', 'Trim',
    ],
    'Pre-Rolls': [
        'Pre-Roll', 'Preroll', 'Pre Roll', 'Blunt', 'Joint',
        'Infused Pre', 'Hash Hole',
    ],
    'Vapes': [
        'Cartridge', 'AIO', 'Disposable', 'Vape', 'Distillate',
        'Live Resin', 'Live Rosin', 'Pod',
    ],
    'Edibles & Tinctures': [
        'Gumm', 'Chocolat', 'Edible', 'Beverage', 'Syrup', 'Tincture',
        'Capsule', 'Drink', 'Cookie', 'Brownie', 'Sucker', 'Chew',
        'Lozenge', 'Mint', 'Tablet',
    ],
    'Concentrates & Topicals': [
        'Concentrate', 'Rosin', 'Hash', 'CNC-', 'Wax', 'Shatter',
        'Diamond', 'Crumble', 'Topical', 'Salve', 'RSO', 'FSO',
        'Live Sugar', 'Badder', 'Sauce',
    ],
    'Featured Deals': [],  # no category restriction — featured promos are brand-wide
}


class PtlDeal(models.Model):
    _name = 'mint.ptl.deal'
    _description = 'PTL Deal — Reusable deal template referenced by PTL days'
    _inherit = [
        'mail.thread', 'mail.activity.mixin',
        'mint.discount.core.mixin', 'mint.bogo.spec.mixin',
        'mint.vendor.funding.mixin', 'mint.weight.parsed.mixin',
    ]
    _order = 'sequence, id'

    name = fields.Char(string='Deal Name', required=True, tracking=True)
    dutchie_publish_owner = fields.Selection(
        [('submission', 'Submission convert'), ('ptl', 'PTL publish')],
        string='Dutchie Publish Owner', copy=False, tracking=True,
        help="Which publish path owns this deal's live Dutchie discount. Set by "
             "the first path to publish live; the other path then refuses to "
             "create a duplicate (#2 cross-path mutex). Clear it to switch paths.")
    brand_id = fields.Many2one(
        'mint.brand',
        string='Brand',
        tracking=True,
        compute='_compute_brand_id',
        store=True,
        readonly=False,
        help='Auto-linked from the deal name when null (matches the segment '
             'before " - " / " · " / ":" against existing mint.brand records, '
             'creating a new brand only if no match). Manually editable.',
    )
    brand_ids = fields.Many2many(
        'mint.brand',
        'mint_ptl_deal_brand_rel',
        'deal_id',
        'brand_id',
        string='Brands',
        help='All brands this deal includes (#93635 multi-brand deals). '
             'When set, product matching and the published discount target '
             'every listed brand; brand_id stays the primary for display.',
    )
    product_category = fields.Selection(
        selection=[(k, k) for k in MASTER_CATEGORY_PATTERNS.keys()],
        string='Product Category',
        help='Master category bucket for product matching. Selection labels are '
             'the dict keys of MASTER_CATEGORY_PATTERNS — keep them in sync.',
    )
    product_category_legacy = fields.Char(
        string='Product Category (legacy free-text)',
        help='Original free-text value captured before the 19.0.4.5.6 conversion '
             'to a Selection. Preserved for audit; not shown in standard views.',
    )
    # discount_type, discount_value, original_price come from
    # mint.discount.core.mixin; weight_value/weight_unit from
    # mint.weight.parsed.mixin (see _inherit). Only the PTL-specific help text
    # is re-declared here.
    discount_value = fields.Float(
        help='For points_multiplier, this is the points multiplier (e.g. 2.0 = 2x points).',
    )
    original_price = fields.Float(
        help='Manufacturer suggested retail price — used to compute display text',
    )
    sales_details = fields.Text(
        string='Sales Details',
        help='Formatted pricing text displayed to customers (PTL Column D)',
    )
    # Structured bundle tiers (#93677): "2 for $18 or 3 for $25" as
    # (qty, price) rows rendered literally — never per-unit math. The
    # structured BOGO triple comes from mint.bogo.spec.mixin.
    bundle_tier_ids = fields.One2many(
        'mint.ptl.deal.bundle.tier',
        'ptl_deal_id',
        string='Bundle Tiers',
    )
    sale_type = fields.Selection(
        selection=[
            ('edlp', 'EDLP'),
            ('weekly_bundle', 'Weekly Bundle'),
            ('special_offer', 'Special Offer'),
            ('bogo', 'BOGO'),
        ],
        string='Sale Type',
        help='PTL Column E — sale classification',
    )
    details_exclusions = fields.Text(
        string='Details & Exclusions',
        help='Product details, exclusions, and conditions (PTL Column C)',
    )
    excluded_skus = fields.Text(
        string='Excluded SKUs',
        help='Newline- or comma-separated SKUs to exclude from this deal. '
             'Matching is case-insensitive against product default_code.',
    )
    excluded_brand_ids = fields.Many2many(
        'mint.brand',
        'mint_ptl_deal_excluded_brand_rel',
        'deal_id',
        'brand_id',
        string='Excluded Brands',
        help='Products from these brands are excluded from this deal.',
    )
    excluded_category_ids = fields.Many2many(
        'product.category',
        'mint_ptl_deal_excluded_categ_rel',
        'deal_id',
        'categ_id',
        string='Excluded Categories',
        help='Categories excluded from this deal, carried from the vendor '
             'submission. Forwarded to the linked mint.discount '
             '(exclude_category_ids): filters the storefront (mintinvsvc '
             'webhook isExclusion flag + server-side applies_to_product) and '
             'emits a Dutchie negative category restriction when no positive '
             'category scope is set — full parity with excluded_brand_ids.',
    )
    excluded_weight_keys = fields.Json(
        string='Excluded Weights',
        default=list,
        help='Weights excluded from this deal (canonical "<value>:<unit>" '
             'keys), carried from the vendor submission Exclusions picker for '
             'reference. On the submission it narrows the Specific Products '
             'picker; it is NOT emitted as a discount/Dutchie weight '
             'restriction (Dutchie weight restrictions are inclusion-only, '
             'driven by weight_value/weight_unit), so unlike excluded '
             'brands/categories it does not reach the live deal post-conversion.',
    )
    store_ids = fields.Many2many(
        'res.company',
        'mint_ptl_deal_store_rel',
        'deal_id',
        'company_id',
        string='Available Stores',
        help='Stores where this deal is available. Leave empty for all stores.',
    )
    market_id = fields.Many2one(
        'mint.region',
        string='Market',
        tracking=True,
        help='Market/region this deal targets',
    )
    pricing_tier = fields.Selection(
        selection=[
            ('standard', 'Standard'),
            ('tourist', 'Tourist'),
            ('local', 'Local'),
        ],
        string='Pricing Tier',
        default='standard',
        help='Nevada pricing tier differentiation (tourist vs local)',
    )
    description = fields.Text(string='Description')
    sequence = fields.Integer(string='Sequence', default=10)
    is_featured = fields.Boolean(string='Featured')
    is_available_online = fields.Boolean(
        string='Available Online',
        default=True,
        tracking=True,
        help='When True the deal is advertised on the ecommerce site and daily-deals '
             'sheets. When False the deal is POS/register-only — it still applies at the '
             'point of sale but is hidden from all online advertising surfaces. Mirrors '
             "Dutchie's IsAvailableOnline flag.",
    )

    # --- State machine ---
    state = fields.Selection(
        selection=[
            ('pending', 'Pending Review'),
            ('approved', 'Approved'),
            ('rejected', 'Rejected'),
            ('live', 'Live'),
            ('expired', 'Expired'),
        ],
        string='Status',
        default='pending',
        tracking=True,
    )
    reviewed_by = fields.Many2one('res.users', string='Reviewed By', readonly=True)
    reviewed_at = fields.Datetime(string='Reviewed At', readonly=True)
    submitted_by = fields.Many2one('res.users', string='Submitted By', readonly=True)
    submitted_at = fields.Datetime(string='Submitted At', readonly=True)
    rejection_reason = fields.Text(string='Rejection Reason')

    # --- Display ---
    display_text = fields.Char(
        string='Display Text',
        compute='_compute_display_text',
        store=True,
        help='Auto-formatted pricing display: ~~$MSRP~~ $SALE | X% Off',
    )
    format_key = fields.Char(
        string='Format Key',
        compute='_compute_format_key',
        store=True,
        index=True,
        help='Rollup key for the public PTL: Brand + Product Line + Weight + rounded '
             'MSRP. Collapses strain variants of the same format into a single PTL '
             'row. Blank until a matching product carries a Product Line '
             '(product.template.x_product_line, backfilled in a later phase); while '
             'blank the storefront falls back to the deal name so behavior is '
             'unchanged for unmastered products.',
    )

    # --- Revoke audit (set by mint.deal.revoke.wizard) ---
    previously_revoked = fields.Boolean(
        string='Previously Revoked',
        default=False,
        tracking=True,
        help='True if this deal has had at least one revoke action applied '
             'against it. Surface in the kanban as a warning badge so '
             'operators know to read the chatter before re-plotting.',
    )
    revoked_at = fields.Datetime(string='Last Revoked At', readonly=True)
    revoked_by = fields.Many2one('res.users', string='Last Revoked By', readonly=True)
    last_revoke_scope = fields.Selection(
        selection=[
            ('single_day', 'Single Day'),
            ('from_date_forward', 'From Date Forward'),
            ('all_future_with_requeue', 'All Future + Re-queue'),
            ('all_instances', 'All Instances'),
        ],
        string='Last Revoke Scope',
        readonly=True,
    )

    # --- Stock check ---
    stock_status = fields.Selection(
        selection=[
            ('unchecked', 'Not Checked'),
            ('in_stock', 'In Stock'),
            ('low_stock', 'Low Stock'),
            ('out_of_stock', 'Out of Stock'),
        ],
        string='Stock Status',
        default='unchecked',
    )
    stock_locations_in = fields.Integer(string='Locations In Stock', default=0)
    stock_locations_total = fields.Integer(string='Total Locations', default=0)
    stock_checked_at = fields.Datetime(string='Last Stock Check')

    # vendor_funding_amount / vendor_funding_percent / currency_id (carried
    # forward from submission/campaign) come from mint.vendor.funding.mixin.
    campaign_id = fields.Many2one(
        'mint.national.promo',
        string='Campaign',
        ondelete='set null',
        index=True,
        tracking=True,
    )

    # --- Relations ---
    discount_id = fields.Many2one(
        'mint.discount',
        string='Discount Record',
        help='The mint.discount record synced to Redis for this deal.',
        ondelete='set null',
    )
    day_ids = fields.Many2many(
        'mint.ptl.day',
        'mint_ptl_day_deal_rel',
        'deal_id',
        'day_id',
        string='PTL Days',
    )
    day_count = fields.Integer(
        string='Days Active',
        compute='_compute_day_count',
    )

    # --- Matching products (resolved from brand + master category) ---
    matching_product_ids = fields.Many2many(
        'product.template',
        compute='_compute_matching_products',
        string='Matching Products',
        help='Live product.template records this deal will apply to, '
             'resolved from brand_id + product_category at form open, then '
             'scoped to the deal\'s market (store_ids, else market_id stores) '
             'via product.template.x_location_id so an AZ deal never lists a '
             'same-brand MO/IL SKU (Odoo #587). '
             'Mirrors the matching used by mint.discount._ensure (brand_ids + '
             'category_ids resolved via product.category name match). '
             'When explicit_product_ids is populated, equals that set '
             '(intersected with brand_id and the market scope) instead.',
    )
    matching_product_count = fields.Integer(
        string='# Matching SKUs',
        compute='_compute_matching_products',
    )
    explicit_product_ids = fields.Many2many(
        'product.template',
        'mint_ptl_deal_explicit_product_rel',
        'deal_id',
        'product_id',
        string='Explicit Products',
        help='When populated, _compute_matching_products returns exactly '
             'this set (intersected with brand_id) and _deal_to_discount_vals '
             'forwards it to mint.discount.product_ids so the Dutchie '
             'discount restricts at the SKU level. Empty = use today\'s '
             'brand+category+excluded_skus fallback.',
    )

    # --- Validity range ---
    date_start = fields.Date(
        string='Start Date',
        tracking=True,
        help='First day this deal is valid. Used for forecasting and the '
             '"Live"/"Expired" auto-state transitions.',
    )
    date_end = fields.Date(
        string='End Date',
        tracking=True,
        help='Last day this deal is valid (inclusive).',
    )
    date_range_label = fields.Char(
        string='Range',
        compute='_compute_date_range_label',
        store=False,
    )
    is_active_today = fields.Boolean(
        string='Active Today',
        compute='_compute_is_active_today',
        search='_search_is_active_today',
        help='True when today falls within date_start..date_end and state != expired/rejected.',
    )

    # --- Computed fields ---

    def _resolve_master_categories(self):
        """Resolve product_category text to a product.category recordset.

        For master-bucket cats (Flower / Pre-Rolls / Vapes / Edibles & Tinctures /
        Concentrates & Topicals / Featured Deals), expand to the configured
        name fragments so e.g. "Flower" includes "Prepack Flower", "Bulk Flower",
        etc. For any other (legacy) text, fall back to an exact ilike name match.

        Returns an empty recordset when product_category is empty OR the master
        bucket is "Featured Deals" (which intentionally has no category limit).
        """
        self.ensure_one()
        Category = self.env['product.category'].sudo()
        if not self.product_category:
            return Category.browse([])
        patterns = MASTER_CATEGORY_PATTERNS.get(self.product_category)
        if patterns is None:
            # Legacy / non-master text — exact name match
            return Category.search([('name', '=ilike', self.product_category)])
        if not patterns:
            # Master bucket with no patterns (Featured Deals) — no category limit
            return Category.browse([])
        domain = ['|'] * (len(patterns) - 1) + [
            ('name', 'ilike', p) for p in patterns
        ]
        return Category.search(domain)

    def _market_location_guids(self):
        """Dutchie store GUIDs (res.company.x_dutchie_store_id) that scope this
        deal's market. Prefer the explicitly-picked store_ids; fall back to all
        stores in the deal's region (market_id) — mirroring the publish-time
        fallback in ptl_day._push_discounts_to_redis. Returns a set of
        non-empty GUID strings — an empty set means "no stores known", i.e.
        unscoped.

        Each product.template row belongs to exactly one Dutchie location
        (product.template.x_location_id == res.company.x_dutchie_store_id), so
        restricting on this set keeps an AZ deal from listing an MO/IL SKU of
        the same brand (Odoo #587: 'AZ deal pulling up an MO sku').
        """
        self.ensure_one()
        stores = self.store_ids or self.market_id.store_ids
        return {g for g in stores.mapped('x_dutchie_store_id') if g}

    @api.depends('brand_id', 'brand_ids', 'product_category', 'excluded_skus',
                 'explicit_product_ids', 'store_ids', 'market_id')
    def _compute_matching_products(self):
        Template = self.env['product.template'].sudo()
        for rec in self:
            brands = (rec.brand_ids | rec.brand_id) if rec.brand_id else rec.brand_ids
            if not brands:
                rec.matching_product_ids = False
                rec.matching_product_count = 0
                continue
            # Market scope: the Dutchie store GUIDs this deal targets. When
            # empty (no store_ids and no region stores) matching stays
            # market-blind — log it so the gap is visible rather than silent.
            loc_guids = rec._market_location_guids()
            if not loc_guids:
                _logger.warning(
                    'PTL deal %s: no store_ids/market stores — matching is '
                    'market-blind (brand+category only).', rec.id)
            # Explicit set wins when populated — intersect with the brand set
            # (so a stale-brand pick doesn't sneak through) AND the market
            # scope (so a wrong-market SKU can't be hand-picked in either).
            if rec.explicit_product_ids:
                explicit = rec.explicit_product_ids.filtered(
                    lambda p: p.brand_id in brands
                    and (not loc_guids or p.x_location_id in loc_guids)
                )
                rec.matching_product_ids = explicit
                rec.matching_product_count = len(explicit)
                continue
            domain = [('brand_id', 'in', brands.ids)]
            if rec.product_category:
                cats = rec._resolve_master_categories()
                if cats:
                    domain.append(('categ_id', 'in', cats.ids))
            if loc_guids:
                domain.append(('x_location_id', 'in', list(loc_guids)))
            tmpls = Template.search(domain)
            if rec.excluded_skus and tmpls:
                tokens = {
                    t.strip().lower()
                    for t in rec.excluded_skus.replace(',', '\n').split('\n')
                    if t.strip()
                }
                if tokens:
                    tmpls = tmpls.filtered(
                        lambda t: (t.default_code or '').lower() not in tokens
                    )
            rec.matching_product_ids = tmpls
            rec.matching_product_count = len(tmpls)

    @api.depends('day_ids')
    def _compute_day_count(self):
        for rec in self:
            rec.day_count = len(rec.day_ids)

    @api.depends('date_start', 'date_end')
    def _compute_date_range_label(self):
        for rec in self:
            if rec.date_start and rec.date_end:
                rec.date_range_label = f"{rec.date_start} → {rec.date_end}"
            elif rec.date_start:
                rec.date_range_label = f"from {rec.date_start}"
            elif rec.date_end:
                rec.date_range_label = f"until {rec.date_end}"
            else:
                rec.date_range_label = ''

    @api.depends('date_start', 'date_end', 'state')
    def _compute_is_active_today(self):
        today = fields.Date.context_today(self)
        for rec in self:
            if rec.state in ('rejected', 'expired'):
                rec.is_active_today = False
                continue
            start_ok = (not rec.date_start) or rec.date_start <= today
            end_ok = (not rec.date_end) or today <= rec.date_end
            rec.is_active_today = bool(start_ok and end_ok)

    def _search_is_active_today(self, operator, value):
        today = fields.Date.context_today(self)
        active_domain = [
            '|', ('date_start', '=', False), ('date_start', '<=', today),
            '|', ('date_end', '=', False), ('date_end', '>=', today),
            ('state', 'not in', ('rejected', 'expired')),
        ]
        if (operator == '=' and value) or (operator == '!=' and not value):
            return active_domain
        return ['!'] + active_domain

    @api.constrains('date_start', 'date_end')
    def _check_date_range(self):
        for rec in self:
            if rec.date_start and rec.date_end and rec.date_start > rec.date_end:
                raise ValidationError(
                    f"Start date ({rec.date_start}) must be on or before "
                    f"end date ({rec.date_end})."
                )

    def cron_expire_past_deals(self):
        """Flip live → expired for deals whose date_end has passed.
        Wire from a daily cron in data/ptl_cron_data.xml if/when desired.
        """
        today = fields.Date.context_today(self)
        expired = self.search([
            ('state', '=', 'live'),
            ('date_end', '!=', False),
            ('date_end', '<', today),
        ])
        if expired:
            expired.write({'state': 'expired'})
            expired._deactivate_in_dutchie_on_expire()
        return len(expired)

    # ─── Multi-window plotting (drives the submission-form picker) ───────

    def _resolve_market_id(self, market_id=None):
        """Return an int market_id from the arg or self.market_id.
        Raises UserError when neither is available."""
        self.ensure_one()
        mid = market_id or (self.market_id.id if self.market_id else False)
        if not mid:
            from odoo.exceptions import UserError
            raise UserError(
                "Cannot plot/unplot windows without a market — set market_id "
                "on the deal or pass market_id explicitly."
            )
        return int(mid)

    def action_plot_windows(self, dates, market_id=None):
        """Plot this deal onto every (date, market) day in `dates`.

        `dates` is a list of ISO-format strings (YYYY-MM-DD). For each date we
        upsert a mint.ptl.day (the date_market_uniq SQL constraint guarantees
        idempotency) and link this deal into its deal_ids via the existing
        mint.ptl.day.schedule_deal primitive — which already returns the
        resulting day_id, so no follow-up search is needed.

        Returns the list of resulting mint.ptl.day ids.
        """
        self.ensure_one()
        Day = self.env['mint.ptl.day']
        mid = self._resolve_market_id(market_id)
        return [
            Day.schedule_deal(deal_id=self.id, date=d, market_id=mid)['day_id']
            for d in dates
        ]

    def action_unplot_windows(self, dates, market_id=None):
        """Inverse of action_plot_windows: unlink this deal from each day.

        Leaves the mint.ptl.day rows in place (they may carry other deals).
        Returns the list of affected mint.ptl.day ids.
        """
        self.ensure_one()
        Day = self.env['mint.ptl.day']
        mid = self._resolve_market_id(market_id)
        days = Day.search([
            ('date', 'in', list(dates)),
            ('market_id', '=', mid),
            ('deal_ids', 'in', self.id),
        ])
        if days:
            days.write({'deal_ids': [(3, self.id)]})
        return days.ids

    def action_select_all_stores(self):
        """Populate store_ids with every active dispensary that has a Dutchie
        ID — same domain as the store_ids field on the form. Lets a reviewer
        materialize the full list explicitly when "leave empty for all" isn't
        the workflow they want (task #93657).

        Reads through sudo so the button's "Select All" contract holds for
        non-admin PTL managers whose res.users.company_ids doesn't cover
        every dispensary company — without sudo the implicit env.companies
        filter on res.company would silently return only the user's subset.
        """
        self.ensure_one()
        Store = self.env['res.company'].sudo()
        stores = Store.search([
            ('is_dispensary', '=', True),
            ('dutchie_store_id', '!=', False),
        ])
        # The m2m write needs sudo too: Odoo validates the user has READ access
        # to each target record on assignment, and group_ptl_manager users
        # typically don't have res.company read on every dispensary. Using
        # self.sudo() narrows the elevation to this one field write — the user
        # still owns the parent deal.
        self.sudo().store_ids = [(6, 0, stores.ids)]
        return True

    def _weight_source(self):
        return (self.name, self.sales_details)

    @api.depends('name', 'sales_details')
    def _compute_weight(self):
        # Source fields + deps are PTL-specific; the parse/assign body lives in
        # mint.weight.parsed.mixin.
        return super()._compute_weight()

    @api.depends('name')
    def _compute_brand_id(self):
        # Skip when brand_id is already set — respects manual edits and
        # avoids overwriting bulk-imported links. Batches the brand lookup
        # so we hit the DB once per compute pass regardless of recordset size.
        candidates = []
        for rec in self:
            if rec.brand_id:
                continue
            text = _parse_brand_name(rec.name)
            if text:
                candidates.append((rec, text))
        if not candidates:
            return
        Brand = self.env['mint.brand']
        # Load all brands once and index by normalized key (lowercase, trimmed,
        # punct + whitespace stripped). Skip tombstoned [MERGED→...] records.
        # Without normalization, "Tru Infusion" doesn't match "TRU-Infusion"
        # and "Cresco" doesn't match "Cresco " (trailing space), creating dupes.
        by_norm = {}
        for b in Brand.search([]):
            if b.name and b.name.startswith('[MERGED→'):
                continue
            k = _brand_lookup_key(b.name)
            if k and k not in by_norm:
                by_norm[k] = b
        for rec, text in candidates:
            key = _brand_lookup_key(text)
            brand = by_norm.get(key)
            if not brand:
                brand = Brand.create({'name': text})
                by_norm[key] = brand
            rec.brand_id = brand.id

    @api.depends('discount_type', 'discount_value', 'original_price',
                 'sales_details', 'bogo_buy_qty', 'bogo_get_qty',
                 'bogo_get_pct', 'bundle_tier_ids.qty',
                 'bundle_tier_ids.price', 'bundle_tier_ids.sequence')
    def _compute_display_text(self):
        for rec in self:
            # If sales_details is manually set, prefer it
            if rec.sales_details:
                rec.display_text = rec.sales_details
                continue

            msrp = rec.original_price
            val = rec.discount_value
            dtype = rec.discount_type

            if dtype == 'percent' and val:
                pct = val if val > 1 else val * 100
                if msrp:
                    sale = msrp * (1 - pct / 100)
                    rec.display_text = f"~~${msrp:.0f}~~ ${sale:.2f} | {pct:.0f}% Off"
                else:
                    rec.display_text = f"{pct:.0f}% Off"
            elif dtype == 'fixed' and val:
                if msrp:
                    sale = msrp - val
                    rec.display_text = f"~~${msrp:.0f}~~ ${sale:.2f} | ${val:.0f} Off"
                else:
                    rec.display_text = f"${val:.0f} Off"
            elif dtype == 'price' and val:
                if msrp:
                    rec.display_text = f"~~${msrp:.0f}~~ ${val:.2f}"
                else:
                    rec.display_text = f"${val:.2f}"
            elif dtype == 'bogo':
                # Structured triple (#93677) renders literally, e.g.
                # "B1G1 50% Off"; un-backfilled deals keep the legacy text.
                structured = rec._bogo_sales_text()
                if structured:
                    rec.display_text = (
                        f"Starting @ ${msrp:.0f} | {structured}" if msrp
                        else structured
                    )
                elif msrp:
                    rec.display_text = f"Starting @ ${msrp:.0f} | BOGO"
                else:
                    rec.display_text = "Buy One Get One"
            elif dtype == 'bundle':
                # Tiers render literally with NO per-unit MSRP math (#93677):
                # "2 for $18 or 3 for $25". Keyed on the FORMATTED text, not
                # tier existence — all-zero tier rows fall through to the
                # legacy discount_value rendering instead of blanking the
                # display.
                tiers_text = format_bundle_tiers_text(
                    [(t.qty, t.price) for t in rec.bundle_tier_ids])
                if tiers_text:
                    rec.display_text = tiers_text
                elif val and msrp:
                    rec.display_text = f"${msrp:.0f} Value! Only ${val:.2f}"
                elif val:
                    rec.display_text = f"${val:.2f} Bundle"
                else:
                    rec.display_text = ''
            elif dtype == 'points_multiplier' and val:
                mult = val if val >= 1 else 1 / val if val else 0
                if mult == int(mult):
                    rec.display_text = f"{int(mult)}x Points"
                else:
                    rec.display_text = f"{mult:.1f}x Points"
            elif dtype == 'clearance':
                pct = (val if val > 1 else val * 100) if val else 0
                if msrp and pct:
                    sale = msrp * (1 - pct / 100)
                    rec.display_text = f"Clearance: ~~${msrp:.0f}~~ ${sale:.2f} | {pct:.0f}% Off"
                elif pct:
                    rec.display_text = f"Clearance: {pct:.0f}% Off"
                else:
                    rec.display_text = "Clearance"
            else:
                rec.display_text = ''

    @api.depends('brand_id', 'product_category', 'excluded_skus',
                 'explicit_product_ids', 'weight_value', 'weight_unit',
                 'original_price')
    def _compute_format_key(self):
        for rec in self:
            rec.format_key = rec._build_format_key()

    def _build_format_key(self):
        """Brand + Product Line + Weight + rounded MSRP.

        Returns False unless both a brand and a resolvable product line are
        present — a "format" isn't well-defined without the line, and the
        storefront's `format_key || name` fallback then preserves the current
        per-deal display until x_product_line is backfilled (phase A3).

        Note: the product line is read from the matching products, so a deal's
        format_key does NOT auto-recompute when x_product_line changes on a
        product after the fact. The A3 backfill recomputes affected deals
        explicitly after writing x_product_line.
        """
        self.ensure_one()
        if not self.brand_id:
            return False
        line = self._resolve_format_line()
        if not line:
            return False
        parts = [(self.brand_id.name or '').strip(), line]
        if self.weight_value:
            parts.append(f"{self.weight_value:g}{self.weight_unit or ''}")
        if self.original_price:
            parts.append(f"${round(self.original_price)}")
        return ' '.join(p for p in parts if p)

    def _resolve_format_line(self):
        """Cheapest path to the format's product line (Brand + Line + …).

        format_key needs exactly ONE x_product_line, but reading it via
        matching_product_ids forced a full product.template search per deal
        (returning every brand+category SKU) just to grab the first line —
        the operation that saturated Odoo during the A3 format_key backfill.
        Resolve it directly instead:
          - explicit set: scan the (small) in-memory recordset;
          - brand[+category]: a single indexed `limit=1` search for the first
            product that actually carries a line.

        The product line is a format-level property (shared across a format's
        SKUs), so this returns the same value as scanning the full matching
        set. It deliberately skips the excluded_skus post-filter: excluding a
        specific SKU does not change the format's line. Returns '' when no
        line is resolvable (deal then falls back to its name on the storefront).
        """
        self.ensure_one()
        if not self.brand_id:
            return ''
        if self.explicit_product_ids:
            for prod in self.explicit_product_ids:
                if prod.brand_id == self.brand_id and prod.x_product_line:
                    return prod.x_product_line.strip()
            return ''
        domain = [
            ('brand_id', '=', self.brand_id.id),
            ('x_product_line', '!=', False),
            ('x_product_line', '!=', ''),
        ]
        if self.product_category:
            cats = self._resolve_master_categories()
            if cats:
                domain.append(('categ_id', 'in', cats.ids))
        hit = self.env['product.template'].sudo().search_read(
            domain, ['x_product_line'], limit=1)
        return (hit[0]['x_product_line'] or '').strip() if hit else ''

    @api.model
    def _cron_fill_format_key(self, batch=300):
        """One-shot batched backfill of the stored format_key for existing deals.

        Computing format_key for all ~7.6k deals at upgrade time is too slow (a
        product.template.search per deal over ~62k products) and rolled the
        module upgrade back, so pre-migrate.py pre-creates the column
        (suppressing the ORM mass-recompute) and this cron fills it in batches.

        Drains by an id watermark in ir.config_parameter rather than a
        "format_key is empty" filter: many deals legitimately compute to an
        empty key (no whitelisted product line) and would otherwise be
        re-selected forever. Disables itself (ir_cron_fill_format_key) once it
        runs past the last deal. New deals get format_key via the normal
        @api.depends compute on write, so the cron is genuinely one-shot.
        """
        Param = self.env['ir.config_parameter'].sudo()
        last_id = int(Param.get_param('mint_cc.format_key_fill_last_id', '0'))
        deals = self.search(
            [('id', '>', last_id), ('brand_id', '!=', False)],
            order='id', limit=batch,
        )
        if not deals:
            cron = self.env.ref(
                'mint_command_center.ir_cron_fill_format_key',
                raise_if_not_found=False)
            if cron and cron.active:
                cron.active = False
            _logger.info("fill_format_key: backfill complete — cron disabled")
            return
        deals.invalidate_recordset(['format_key'])
        deals._compute_format_key()
        deals.flush_recordset(['format_key'])
        Param.set_param('mint_cc.format_key_fill_last_id', str(deals[-1].id))
        _logger.info(
            "fill_format_key: processed %s deal(s) up to id %s",
            len(deals), deals[-1].id)

    @api.model
    def action_review_recompute_format_keys(self):
        """Re-arm the batched format_key backfill after curating product lines.

        format_key does NOT auto-recompute when x_product_line changes on a
        product (see _build_format_key), so marketing runs this after Product
        Line Review. It does NOT recompute synchronously: a full in-request
        recompute (a product.template.search per deal over ~62k products) is
        the operation that previously rolled back a module upgrade (see
        _cron_fill_format_key). Instead it resets the id watermark and
        re-enables the one-shot batched cron (batch=300), which re-drains every
        deal in the background and disables itself when done — perf-safe and
        re-runnable.
        """
        Param = self.env['ir.config_parameter'].sudo()
        Param.set_param('mint_cc.format_key_fill_last_id', '0')
        cron = self.env.ref(
            'mint_command_center.ir_cron_fill_format_key',
            raise_if_not_found=False)
        if cron:
            cron.active = True
        pending = self.search_count(
            [('format_key', 'in', [False, '']), ('brand_id', '!=', False)])
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Format-Key Backfill Re-armed',
                'message': (
                    'The batched backfill will re-drain all deals in the '
                    f'background over the next few minutes ({pending} currently '
                    'have a blank format key). Newly-curated product lines will '
                    'be picked up as it runs.'
                ),
                'type': 'success',
                'sticky': False,
            },
        }

    # --- Revoke wizard launcher ---

    def action_open_revoke_wizard(self):
        """Open the 4-scope Revoke wizard for this deal."""
        self.ensure_one()
        return {
            'name': 'Revoke Deal',
            'type': 'ir.actions.act_window',
            'res_model': 'mint.deal.revoke.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_deal_id': self.id},
        }

    # --- State transition actions ---

    def action_approve(self):
        self.filtered(lambda d: d.state in ('pending', 'rejected')).write({
            'state': 'approved',
            'reviewed_by': self.env.uid,
            'reviewed_at': fields.Datetime.now(),
            'rejection_reason': False,
        })

    def action_reject(self):
        return {
            'name': 'Reject Deal',
            'type': 'ir.actions.act_window',
            'res_model': 'mint.deal.reject.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'active_model': self._name,
                'active_ids': self.ids,
            },
        }

    def _dutchie_claim(self, path):
        """Claim this deal for live Dutchie publishing by ``path``
        ('submission' | 'ptl'). Returns True if the path may publish (owner
        unset or already this path) and stamps the owner on first claim; False
        if the OTHER path already owns it (#2 cross-path mutex). Pure decision
        in deal_mixins.dutchie_claim_decision."""
        self.ensure_one()
        may, to_set = dutchie_claim_decision(self.dutchie_publish_owner, path)
        if to_set:
            self.sudo().write({'dutchie_publish_owner': to_set})
        return may

    def action_publish(self):
        """Publish this deal to the storefront (Redis) AND Dutchie POS.

        Replaces the old "Set Live" button, which only flipped state and
        pushed nothing. Reuses the mint.ptl.day engine to (re)build and push
        only THIS deal's discount. Note: marking shared days published can
        widen a co-scheduled deal's weekday set at the next lifecycle cron —
        same semantics as the day-level publish. The storefront/Redis push
        always runs. Dutchie writes are
        gated by mint.dutchie_discount_push.mode (off | dry-run | live) plus
        per-market and per-store flags: with the default 'dry-run' the Dutchie
        payload is built and logged to mint.dutchie.discount.push.log but no
        HTTP call fires. Only mode='live' (+ enabled market/stores) writes to
        real Dutchie POS.
        """
        Day = self.env['mint.ptl.day']
        Discount = self.env['mint.discount']
        for deal in self.filtered(lambda d: d.state in ('approved', 'live')):
            if not deal.day_ids:
                raise UserError(
                    "“%s” isn't scheduled on any PTL day yet — add it to a "
                    "day (or set start/end dates and plot it) before "
                    "publishing." % deal.name
                )

            # Day-of-week booleans (_recompute_day_booleans) and the
            # Redis/Dutchie push helpers both require the deal's days to be in
            # state='published'. Mark just this deal's days; we do NOT call the
            # day-level action_publish, so other deals on these days stay as-is.
            deal.day_ids.filtered(lambda d: d.state != 'published').write(
                {'state': 'published'}
            )

            # Build/refresh ONLY this deal's discount (the build itself is
            # market-agnostic, so any day works as the context here).
            discount = deal.day_ids[0]._ensure_discount(deal)
            Discount._recompute_day_booleans(discount)

            # The push helpers scope to a single day's market_id, and a deal
            # can be plotted into days of more than one market. Push once per
            # distinct market, using a representative day from each, so no
            # market's stores are silently skipped.
            Log = self.env['mint.dutchie.discount.push.log'].sudo()
            # Watermark the log id BEFORE the push so the backoffice links below
            # can be scoped to rows created by THIS publish. Use the monotonic,
            # transaction-visible id rather than create_date — create_date is
            # DB-clocked while a wall-clock cutoff would be app-clocked, and the
            # skew between the two servers intermittently dropped the link.
            last_log_id = Log.search([], order='id desc', limit=1).id or 0
            for market in deal.day_ids.mapped('market_id'):
                mday = deal.day_ids.filtered(lambda d: d.market_id == market)[:1]
                mday._push_discounts_to_redis(discount.ids)
                mday._push_discounts_to_dutchie(discount.ids)

            deal.write({'state': 'live'})

            mode = Day._get_dutchie_push_mode()
            note = (
                " (LIVE Dutchie write)" if mode == 'live'
                else " (Dutchie simulated — payload logged, not sent)"
            )
            # Build as Markup so the chatter renders HTML (bold + the link);
            # message_post escapes a plain str body in Odoo 19. The `%` operator
            # on Markup auto-escapes the substituted values (mode/note/store
            # name/url), so no injection from those.
            body = Markup(
                "Published to storefront + Dutchie across %d day(s). "
                "Push mode: <b>%s</b>%s."
            ) % (len(deal.day_ids), mode, note)
            # Append Dutchie backoffice review link(s). Only present after a
            # successful live push — the URL needs the Dutchie discount id, so
            # dry-run / failed pushes contribute nothing here. Scoped to logs
            # created by THIS publish via the id watermark above.
            logs = Log.search([
                ('id', '>', last_log_id),
                ('discount_id', '=', discount.id),
                ('backoffice_url', '!=', False),
            ], order='id desc')
            seen, links = set(), []
            for lg in logs:
                if lg.backoffice_url in seen:
                    continue
                seen.add(lg.backoffice_url)
                links.append(Markup(
                    '<a href="%s" target="_blank">Review in Dutchie backoffice — %s</a>'
                ) % (lg.backoffice_url, lg.company_id.name or 'store'))
            if links:
                body += Markup('<br/>') + Markup('<br/>').join(links)
            deal.message_post(body=body, message_type='comment')

    # Backward-compat alias: the old button name / any automations that still
    # call action_set_live now route through the full publish path.
    def action_set_live(self):
        return self.action_publish()

    def action_open_publish_review(self):
        """Open the pre-publish review wizard (guards + alerts) before the
        deal's Dutchie/storefront publish. Warn-only: shows the target stores
        and any SOP/safety alerts; action_publish fires on confirm."""
        self.ensure_one()
        from .dutchie_publish_review import build_review_html
        mode, is_live, lines, warnings, blocks = self._dutchie_publish_review_data()
        wiz = self.env['mint.dutchie.publish.review'].create({
            'res_model': self._name,
            'res_id': self.id,
            'mode': mode,
            'is_live': is_live,
            'review_html': build_review_html(mode, is_live, lines, warnings, blocks),
        })
        return {
            'type': 'ir.actions.act_window',
            'name': 'Review before publishing',
            'res_model': 'mint.dutchie.publish.review',
            'res_id': wiz.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def _dutchie_publish_review_data(self):
        """Compute (mode, is_live, lines, warnings, blocks) for the deal
        publish WITHOUT writing anything (used by the review wizard)."""
        self.ensure_one()
        Day = self.env['mint.ptl.day']
        Company = self.env['res.company'].sudo()
        mode = Day._get_dutchie_push_mode()
        is_live = mode == 'live'
        lines, warnings, blocks = [], [], []
        lines.append("Deal: %s" % (self.name or ''))
        lines.append("Discount: %s, value %s" % (self.discount_type, self.discount_value))
        lines.append("Storefront (Redis) push ALWAYS runs — not gated by the Dutchie mode.")
        lines.append("Dutchie push mode: %s" % mode)
        if not self.day_ids:
            blocks.append("Not scheduled on any PTL day — Publish would raise.")
            return mode, is_live, lines, warnings, blocks

        today = fields.Date.today()
        markets = self.day_ids.mapped('market_id')
        lines.append("Market(s): %s" % (', '.join(markets.mapped('name')) or '(none)'))
        dates = self.day_ids.mapped('date')
        if dates:
            lines.append("Date range: %s → %s" % (min(dates), max(dates)))
            if max(dates) < today:
                warnings.append("All scheduled days are in the PAST — expired-on-arrival, will not apply.")
            elif min(dates) > today:
                warnings.append("Starts in the FUTURE (%s) — but the storefront push commits immediately on confirm." % min(dates))
            if not any(today <= d <= today + timedelta(days=60) for d in dates):
                warnings.append("No scheduled day is within the next 60 days — Dutchie day-of-week flags compute as 'every day'.")

        if not self.brand_id and not self.product_category and not self.explicit_product_ids:
            warnings.append("No brand / category / product restriction — the discount would apply STORE-WIDE.")

        enabled = Company.search([
            ('is_dispensary', '=', True),
            ('dutchie_store_id', '!=', False),
            ('region_id', 'in', markets.ids),
            ('dutchie_discount_push_enabled', '=', True),
        ])
        targeted = (self.store_ids & enabled) if self.store_ids else enabled
        lines.append("Stores that will receive the Dutchie write: %d — %s"
                     % (len(targeted), ', '.join(targeted.mapped('name')) or 'NONE'))

        if not markets.filtered('dutchie_discount_push_enabled'):
            warnings.append("No market is enabled for the Dutchie push — nothing writes to Dutchie even in live mode (storefront still updates).")
        if not targeted:
            warnings.append("0 stores are enabled for the Dutchie push — this deal goes to the storefront only.")
        if self.store_ids:
            skipped = self.store_ids - targeted
            if skipped:
                warnings.append("%d of the deal's %d target store(s) are NOT enabled and will be SKIPPED on Dutchie: %s"
                                % (len(skipped), len(self.store_ids), ', '.join(skipped.mapped('name'))))
        elif targeted:
            warnings.append("No explicit store scoping on this deal — it publishes to ALL %d enabled store(s) in the market." % len(targeted))
        return mode, is_live, lines, warnings, blocks

    def action_expire(self):
        to_expire = self.filtered(lambda d: d.state in ('approved', 'live'))
        to_expire.write({'state': 'expired'})
        to_expire._deactivate_in_dutchie_on_expire()

    def _deactivate_in_dutchie_on_expire(self):
        """Pull each deal's discount down from Dutchie (IsDeleted=True).

        Mirror of action_publish's Dutchie push: expiring a deal in Odoo must
        also stop the discount in Dutchie, otherwise it keeps running at the
        register/online until its own ValidDateTo. Covers BOTH publish paths:
          • the day/discount path (mint.dutchie_discount_push.mode), via the
            linked mint.discount and its per-store push log, and
          • the submission path (dutchie.publish.mode — the one live in prod),
            via each linked submission's recorded Dutchie ids.
        No-op for deals never pushed live; each side is gated by its own mode
        param and wrapped so one failure can't block the rest.
        """
        if not self:
            return
        discounts = self.mapped('discount_id')
        if discounts:
            try:
                self.env['mint.ptl.day'].sudo()._deactivate_discounts_in_dutchie(discounts)
            except Exception as e:  # noqa: BLE001
                _logger.warning('Expire: day-path Dutchie deactivate failed: %s', e)
        subs = self.env['mint.deal.submission'].sudo().search([('deal_id', 'in', self.ids)])
        for sub in subs:
            try:
                sub._dutchie_deactivate()
            except Exception as e:  # noqa: BLE001
                _logger.warning('Expire: submission Dutchie deactivate failed for %s: %s', sub.id, e)

    def action_reset_to_pending(self):
        self.filtered(lambda d: d.state in ('rejected', 'expired')).write({
            'state': 'pending',
            'rejection_reason': False,
        })

    def action_view_days(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'PTL Days',
            'res_model': 'mint.ptl.day',
            'view_mode': 'list,form',
            'domain': [('id', 'in', self.day_ids.ids)],
        }

    # ─── Text Deals line formatter ────────────────────────────────────────

    _STRIKE = '̶'  # Combining long stroke overlay — strikes the preceding glyph

    def _format_pricing_plain(self):
        """Take this deal's display_text (which uses markdown-style ~~strike~~)
        and convert it to a plain-text equivalent with U+0336 combining-strike
        overlays. Falls back to sales_details, then to an empty string.
        """
        import re
        text = self.display_text or self.sales_details or ''
        return re.sub(
            r'~~(.+?)~~',
            lambda m: ''.join(c + self._STRIKE for c in m.group(1)),
            text,
        )

    def _compose_text_deal_line(self):
        """One bud-tender-friendly text line for this deal. Used by
        mint.ptl.day._compose_text_deals() for SMS / POS-paste exports.

        Format: ``{emoji} {brand}: {label} — {strikethrough_pricing}``

        The override on product.template (x_display_label_override) is
        intentionally NOT consulted here: F6c exports at deal level (one
        line per deal), not per-product. The override surfaces in the
        embedded matching-SKU list and any future per-product export.
        """
        self.ensure_one()
        emoji = (self.brand_id.display_emoji if self.brand_id else '') or '🌿'
        brand = self.brand_id.name if self.brand_id else 'Unbranded'
        label = self.name or ''
        pricing = self._format_pricing_plain()
        if pricing:
            return f"{emoji} {brand}: {label} — {pricing}"
        return f"{emoji} {brand}: {label}"

    @api.model
    def search_approved_for_dragboard(self, date_from=None, date_to=None, market_id=None):
        """Return approved deals available to schedule on the PTL Calendar.

        Filters:
          - state == 'approved'
          - If market_id is given, deal.market_id matches or is unset.
          - If date_from/date_to given, the deal's validity window must
            overlap the calendar range. Deals without a window are always
            considered valid.

        Returns a list of dicts the dragboard renders directly.
        """
        domain = [('state', '=', 'approved')]
        if market_id:
            domain += ['|', ('market_id', '=', int(market_id)), ('market_id', '=', False)]
        if date_from:
            domain.append('|')
            domain.append(('date_end', '=', False))
            domain.append(('date_end', '>=', date_from))
        if date_to:
            domain.append('|')
            domain.append(('date_start', '=', False))
            domain.append(('date_start', '<=', date_to))

        deals = self.search(domain, order='sequence, id', limit=200)
        result = []
        for deal in deals:
            result.append({
                'id': deal.id,
                'name': deal.name or '',
                'brand': deal.brand_id.name or '',
                'category': deal.product_category or '',
                'sale_type': dict(deal._fields['sale_type'].selection).get(deal.sale_type, '') if deal.sale_type else '',
                'discount_type': dict(deal._fields['discount_type'].selection).get(deal.discount_type, '') if deal.discount_type else '',
                'discount_value': deal.discount_value or 0.0,
                'display_text': deal.display_text or '',
                'weight_value': deal.weight_value or 0.0,
                'weight_unit': deal.weight_unit or '',
                'market_id': deal.market_id.id if deal.market_id else False,
                'market': deal.market_id.name or '',
                'is_featured': bool(deal.is_featured),
                'date_start': deal.date_start.isoformat() if deal.date_start else False,
                'date_end': deal.date_end.isoformat() if deal.date_end else False,
            })
        return result
