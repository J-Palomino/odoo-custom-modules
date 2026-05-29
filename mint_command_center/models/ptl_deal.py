import logging

from odoo import api, fields, models
from odoo.exceptions import ValidationError

from .brand_calendar import _brand_lookup_key, _parse_brand_name, _parse_weight

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
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'sequence, id'

    name = fields.Char(string='Deal Name', required=True, tracking=True)
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
    discount_type = fields.Selection(
        selection=[
            ('percent', 'Percentage Off'),
            ('fixed', 'Fixed Amount Off'),
            ('bogo', 'BOGO'),
            ('bundle', 'Bundle Deal'),
            ('price', 'Set Price'),
            ('points_multiplier', 'Loyalty Points Multiplier'),
            ('clearance', 'Clearance (Near Expiry)'),
        ],
        string='Discount Type',
    )
    discount_value = fields.Float(
        string='Discount Value',
        help='For points_multiplier, this is the points multiplier (e.g. 2.0 = 2x points).',
    )
    original_price = fields.Float(
        string='Original / MSRP Price',
        help='Manufacturer suggested retail price — used to compute display text',
    )
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
        selection=[
            ('g', 'g'),
            ('mg', 'mg'),
            ('oz', 'oz'),
            ('ct', 'ct'),
        ],
        string='Unit',
        compute='_compute_weight',
        store=True,
        readonly=False,
        tracking=True,
    )
    sales_details = fields.Text(
        string='Sales Details',
        help='Formatted pricing text displayed to customers (PTL Column D)',
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

    # --- Vendor funding (carried forward from submission/campaign) ---
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
             'resolved from brand_id + product_category at form open. '
             'Mirrors the matching used by mint.discount._ensure (brand_ids + '
             'category_ids resolved via product.category name match). '
             'When explicit_product_ids is populated, equals that set '
             '(intersected with brand_id) instead.',
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

    @api.depends('brand_id', 'product_category', 'excluded_skus', 'explicit_product_ids')
    def _compute_matching_products(self):
        Template = self.env['product.template'].sudo()
        for rec in self:
            if not rec.brand_id:
                rec.matching_product_ids = False
                rec.matching_product_count = 0
                continue
            # Explicit set wins when populated — intersect with brand_id
            # so a stale-brand pick doesn't sneak through.
            if rec.explicit_product_ids:
                explicit = rec.explicit_product_ids.filtered(
                    lambda p: p.brand_id == rec.brand_id
                )
                rec.matching_product_ids = explicit
                rec.matching_product_count = len(explicit)
                continue
            domain = [('brand_id', '=', rec.brand_id.id)]
            if rec.product_category:
                cats = rec._resolve_master_categories()
                if cats:
                    domain.append(('categ_id', 'in', cats.ids))
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

    @api.depends('name', 'sales_details')
    def _compute_weight(self):
        for rec in self:
            value, unit = _parse_weight(rec.name, rec.sales_details)
            rec.weight_value = value
            rec.weight_unit = unit or False

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

    @api.depends('discount_type', 'discount_value', 'original_price', 'sales_details')
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
                if msrp:
                    rec.display_text = f"Starting @ ${msrp:.0f} | BOGO"
                else:
                    rec.display_text = "Buy One Get One"
            elif dtype == 'bundle' and val:
                if msrp:
                    rec.display_text = f"${msrp:.0f} Value! Only ${val:.2f}"
                else:
                    rec.display_text = f"${val:.2f} Bundle"
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
        line = ''
        for prod in self.matching_product_ids:
            if prod.x_product_line:
                line = prod.x_product_line.strip()
                break
        if not line:
            return False
        parts = [(self.brand_id.name or '').strip(), line]
        if self.weight_value:
            parts.append(f"{self.weight_value:g}{self.weight_unit or ''}")
        if self.original_price:
            parts.append(f"${round(self.original_price)}")
        return ' '.join(p for p in parts if p)

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

    def action_set_live(self):
        self.filtered(lambda d: d.state == 'approved').write({'state': 'live'})

    def action_expire(self):
        self.filtered(lambda d: d.state in ('approved', 'live')).write({'state': 'expired'})

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
