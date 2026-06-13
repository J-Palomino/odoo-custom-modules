import logging
from datetime import date as _date, timedelta

from odoo import api, fields, models
from odoo.exceptions import UserError

from .deal_mixins import format_bundle_tiers_text
from .ptl_deal import MASTER_CATEGORY_PATTERNS

_logger = logging.getLogger(__name__)


class DealSubmission(models.Model):
    _name = 'mint.deal.submission'
    _description = 'Vendor Deal Submission'
    _inherit = [
        'mail.thread', 'mail.activity.mixin',
        'mint.discount.core.mixin', 'mint.bogo.spec.mixin',
        'mint.vendor.funding.mixin',
    ]
    _order = 'create_date desc'

    # --- CRM linkage ---
    crm_lead_id = fields.Many2one(
        'crm.lead',
        string='CRM Lead',
        ondelete='set null',
        tracking=True,
        help='Source lead (vendor rep conversation) this submission originated from.',
    )
    campaign_id = fields.Many2one(
        'mint.national.promo',
        string='Promo Campaign',
        ondelete='set null',
        tracking=True,
        index=True,
        help='National promo campaign this submission rolls up into.',
    )

    # --- Vendor info ---
    vendor_name = fields.Char(string='Vendor / Brand Name', required=True, tracking=True)
    vendor_email = fields.Char(string='Vendor Email')
    vendor_contact = fields.Char(string='Contact Person')
    vendor_phone = fields.Char(string='Phone')
    brand_id = fields.Many2one(
        'mint.brand',
        string='Brand',
        help='Primary brand. Kept for back-compat (campaign naming, legacy '
             'reports); when Brands below is used, this is its first entry.',
    )
    brand_ids = fields.Many2many(
        'mint.brand',
        'mint_deal_submission_brand_rel',
        'submission_id',
        'brand_id',
        string='Brands',
        help='All brands this deal includes (#93635 multi-brand deals). Two '
             'or more brands are accepted in one submission; the converted '
             'PTL deal and the Dutchie discount carry every selected brand. '
             'Leave empty for single-brand deals (Brand field above).',
    )
    product_ids = fields.Many2many(
        'product.template',
        'mint_deal_submission_product_rel',
        'submission_id',
        'product_id',
        string='Specific Products',
        help='When populated, the resulting PTL deal targets ONLY these '
             'products and ignores the implicit brand+category widening. '
             'Leave empty to keep today\'s "all-of-brand-and-category" '
             'fallback. Picker is brand-scoped via the view domain.',
    )

    # --- Vendor funding terms ---
    # vendor_funding_amount / vendor_funding_percent / currency_id come from
    # mint.vendor.funding.mixin.
    vendor_funding_terms = fields.Text(string='Funding Terms')

    # --- Deal details ---
    name = fields.Char(string='Deal Name', required=True, tracking=True)
    product_category = fields.Char(string='Product Category')
    product_category_id = fields.Many2one(
        'product.category',
        string='In-Stock Category',
        help='Primary in-stock category. Kept for back-compat (mirrors the '
             'brand_id/brand_ids pattern); when In-Stock Categories below is '
             'used, this is its first entry.',
    )
    product_category_ids = fields.Many2many(
        'product.category',
        'mint_deal_submission_categ_rel',
        'submission_id',
        'categ_id',
        string='In-Stock Categories',
        help='All categories this deal includes — limited to categories the '
             'selected brand(s) currently have in stock, same multi-select '
             'pattern as Brands. Picks fill the plain-text Product Category '
             '(comma-separated Dutchie names); downstream (PTL conversion, '
             'stock check, Dutchie publish) keeps reading the text field.',
    )
    available_category_ids = fields.Many2many(
        'product.category',
        compute='_compute_available_category_ids',
        string='Available In-Stock Categories',
        help='Distinct Dutchie categories with on-hand stock '
             '(x_quantity_available > 0) across the selected brand(s). '
             'Drives the In-Stock Category dropdown domain.',
    )
    available_product_ids = fields.Many2many(
        'product.template',
        compute='_compute_available_product_ids',
        string='Available Products',
        help='Products matching every selected facet — brand(s), in-stock '
             'categor(ies), and market/stores. A product qualifies when one '
             'of its variants has on-hand stock at one of the requested '
             'stores (variant x_dutchie_location_id + x_quantity_available '
             'from the Dutchie sync). Drives the Specific Products picker '
             'domain.',
    )
    publish_match_count = fields.Integer(
        string='In-Stock Products Matched',
        compute='_compute_publish_match_count',
        help='How many in-stock products this deal resolves to at the target '
             'stores (explicit Specific Products, else brand ∩ categories). '
             '0 means the deal would be invisible on the storefront and apply '
             'to nothing at the register — conversion is blocked. -1 means no '
             'product-narrowing restriction (store-wide), handled elsewhere.',
    )
    # discount_type / discount_value / original_price come from
    # mint.discount.core.mixin.
    sales_details = fields.Text(
        string='Sales Details',
        help='Formatted pricing text — how this deal should be displayed',
    )
    # Structured bundle tiers (#93677): "2 for $18 or 3 for $25" as
    # (qty, price) rows. Forwarded to the PTL deal on conversion. The
    # structured BOGO triple comes from mint.bogo.spec.mixin.
    bundle_tier_ids = fields.One2many(
        'mint.deal.submission.bundle.tier',
        'submission_id',
        string='Bundle Tiers',
    )
    details_exclusions = fields.Text(
        string='Details & Exclusions',
        help='Product details, exclusions, and conditions',
    )
    excluded_brand_ids = fields.Many2many(
        'mint.brand',
        'mint_deal_submission_excluded_brand_rel',
        'submission_id',
        'brand_id',
        string='Excluded Brands',
        help='Brands to exclude from this deal. Forwarded to the PTL deal '
             '(mint.ptl.deal.excluded_brand_ids) on conversion.',
    )
    excluded_skus = fields.Text(
        string='Excluded SKUs',
        help='SKUs to exclude (one per line or comma-separated). Forwarded to '
             'the PTL deal (mint.ptl.deal.excluded_skus) on conversion.',
    )

    # --- Targeting ---
    market_id = fields.Many2one(
        'mint.region',
        string='Market',
        tracking=True,
    )
    store_ids = fields.Many2many(
        'res.company',
        'mint_deal_submission_store_rel',
        'submission_id',
        'company_id',
        string='Requested Stores',
    )

    # --- Date preferences ---
    preferred_start_date = fields.Date(string='Preferred Start Date')
    preferred_end_date = fields.Date(string='Preferred End Date')
    preferred_days = fields.Char(
        string='Preferred Days of Week',
        help='e.g. Mon, Wed, Fri',
    )

    # --- Plot windows (structured replacement for preferred_start/end) ---
    window_ids = fields.One2many(
        'mint.deal.submission.window',
        'submission_id',
        string='Plot Windows',
        help='Non-contiguous date windows. Replayed onto the resulting '
             'mint.ptl.deal.day_ids when the submission is converted.',
    )
    windows_summary = fields.Char(
        string='Schedule Summary',
        compute='_compute_windows_summary',
        store=True,
    )

    @api.depends('window_ids.date_start', 'window_ids.date_end')
    def _compute_windows_summary(self):
        for rec in self:
            parts = []
            for w in rec.window_ids:
                if not w.date_start or not w.date_end:
                    continue
                if w.date_start == w.date_end:
                    parts.append(w.date_start.strftime('%b %-d'))
                else:
                    same_year = w.date_start.year == w.date_end.year
                    same_month = same_year and w.date_start.month == w.date_end.month
                    if same_month:
                        parts.append(
                            f"{w.date_start.strftime('%b %-d')}–"
                            f"{w.date_end.strftime('%-d')}"
                        )
                    else:
                        parts.append(
                            f"{w.date_start.strftime('%b %-d')}–"
                            f"{w.date_end.strftime('%b %-d')}"
                        )
            rec.windows_summary = ', '.join(parts) or False

    # --- State machine ---
    # Board lifecycle: New -> Under Review -> [Approved] -> Scheduled ->
    # Final Review -> Expired, with Rejected off the happy path.
    #   * Approved stays an internal greenlight (creates the National Promo
    #     campaign and is the gate the plot-check enforces) before a deal can
    #     be plotted.
    #   * Scheduled (formerly "Converted to Deal") = PTL deal created + plotted
    #     onto the calendar = scheduled to go live.
    #   * Final Review (formerly "Live") = the run window has ended; awaiting
    #     human closeout sign-off. The daily cron advances Scheduled here on the
    #     run's last day.
    #   * Expired = closed out (manual sign-off from Final Review).
    state = fields.Selection(
        selection=[
            ('new', 'New'),
            ('under_review', 'Under Review'),
            ('approved', 'Approved'),
            ('scheduled', 'Scheduled'),
            ('final_review', 'Final Review'),
            ('expired', 'Expired'),
            ('rejected', 'Rejected'),
        ],
        string='Status',
        default='new',
        tracking=True,
    )

    # Last day this deal runs — max of the plot windows, falling back to the
    # legacy preferred end date. Drives the Scheduled -> Final Review cron.
    run_end_date = fields.Date(
        string='Run Ends',
        compute='_compute_run_end_date',
        store=True,
    )

    @api.depends('window_ids.date_end', 'preferred_end_date')
    def _compute_run_end_date(self):
        for rec in self:
            ends = [d for d in rec.window_ids.mapped('date_end') if d]
            rec.run_end_date = max(ends) if ends else rec.preferred_end_date

    # --- Linked deal (after conversion) ---
    deal_id = fields.Many2one(
        'mint.ptl.deal',
        string='Converted Deal',
        readonly=True,
    )

    # --- Review tracking ---
    reviewed_by = fields.Many2one('res.users', string='Reviewed By', readonly=True)
    reviewed_at = fields.Datetime(string='Reviewed At', readonly=True)
    rejection_reason = fields.Text(string='Rejection Reason')
    reviewer_notes = fields.Text(string='Reviewer Notes')

    # --- Source / external intake (JotForm import) ---
    # Provenance of the submission. The web form (/vendor-deals) leaves the
    # default 'web_form'; the JotForm importer writes 'jotform'.
    source = fields.Selection(
        selection=[
            ('web_form', 'Web Form'),
            ('jotform', 'JotForm'),
            ('manual', 'Manual'),
        ],
        string='Source',
        default='web_form',
        tracking=True,
    )
    external_id = fields.Char(
        string='External Submission ID',
        index=True,
        copy=False,
        help='Stable ID of the source submission (e.g. JotForm submission id). '
             'Keeps imports idempotent — re-running the importer never '
             'duplicates a submission.',
    )
    external_form_id = fields.Char(
        string='External Form ID',
        help='Source form id (e.g. the JotForm "Promo - Deal submission" form).',
    )
    external_created_at = fields.Char(
        string='External Submitted At',
        help='Original submission timestamp from the source system, verbatim.',
    )
    jotform_payload = fields.Text(
        string='Raw JotForm Payload',
        help='Complete, verbatim capture of every field from the source '
             'submission (JSON). Safety net so no detail is ever lost, even '
             'for questions that have no dedicated column.',
    )

    # --- JotForm-specific fields (structured, so they are queryable) ---
    deal_frequency = fields.Char(
        string='Deal Frequency',
        help='Vendor-stated cadence (Weekly, EDLP, 2x/month, Single Day, '
             'Holiday, Other). Informs PTL scheduling at review time.',
    )
    product_list = fields.Text(
        string='Products (raw, with weights)',
        help='Vendor-supplied product list, verbatim (incl. weights). The '
             'reviewer resolves these into the structured product_ids picker.',
    )
    promo_units = fields.Char(
        string='Promo Units Offered?',
        help='Whether the vendor offers promo/doorbuster units for special '
             'events (Yes/No).',
    )
    promo_units_product = fields.Char(string='Promo Units — Product')
    promo_units_qty = fields.Integer(string='Promo Units — Quantity')
    promo_delivery_date = fields.Date(
        string='Promo Units — Est. Delivery',
        help='Vendor estimate; actual delivery is coordinated with intake.',
    )
    deal_end_note = fields.Char(
        string='Deal End (free-text)',
        help='Vendor end-of-deal note when not a structured date '
             '(e.g. "Q4", "until product runs out").',
    )

    def init(self):
        # Idempotency backstop for the JotForm importer. It does a
        # check-then-create on (source='jotform', external_id); a partial
        # unique index makes the DB the source of truth so two concurrent or
        # retried importer runs can't double-insert one submission. Scoped to
        # jotform rows so web_form / manual rows (external_id NULL) are
        # unaffected. Safe to (re)apply: the column starts all-NULL on upgrade.
        self.env.cr.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS
                mint_deal_submission_jotform_extid_uniq
            ON mint_deal_submission (external_id)
            WHERE source = 'jotform' AND external_id IS NOT NULL
        """)

    # --- Actions ---

    def action_start_review(self):
        self.filtered(lambda s: s.state == 'new').write({
            'state': 'under_review',
        })

    def action_approve(self):
        records = self.filtered(lambda s: s.state in ('new', 'under_review'))
        records.write({
            'state': 'approved',
            'reviewed_by': self.env.uid,
            'reviewed_at': fields.Datetime.now(),
            'rejection_reason': False,
        })
        # Find or create the parent campaign for each approved submission
        Campaign = self.env['mint.national.promo']
        year = _date.today().year
        for sub in records:
            # Multi-brand (#93635): the campaign keys on the PRIMARY brand —
            # first of brand_ids when the single brand_id isn't set.
            primary = sub.brand_id or sub.brand_ids[:1]
            if not sub.campaign_id and primary and sub.market_id:
                target_year = sub.preferred_start_date.year if sub.preferred_start_date else year
                campaign = Campaign.get_or_create(
                    brand_id=primary.id,
                    market_id=sub.market_id.id,
                    year=target_year,
                    crm_lead_id=sub.crm_lead_id.id if sub.crm_lead_id else False,
                )
                if campaign:
                    sub.campaign_id = campaign.id
        # Campaigns are background bookkeeping — never staff-entered and
        # never a blocker. get_or_create births them 'approved'; this batched
        # call heals any pre-existing 'planning' rows (action_approve filters
        # to planning internally, so it's a no-op for the rest).
        records.campaign_id.action_approve()

    def action_reject(self):
        return {
            'name': 'Reject Submission',
            'type': 'ir.actions.act_window',
            'res_model': 'mint.deal.reject.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'active_model': self._name,
                'active_ids': self.ids,
            },
        }

    # ─── Campaign self-heal (replaces the old blocking plot gate) ────────
    #
    # Campaigns (mint.national.promo) are background bookkeeping: they are
    # auto-created on submission approval (born 'approved'), never entered
    # by staff, and their menu is hidden. Per the 2026-06-12 directive they
    # must never block plotting — a leftover 'planning' campaign is
    # auto-approved in place, and a missing campaign is simply not a
    # problem (the rollup link is best-effort).

    def _autoapprove_campaign(self):
        """Heal a legacy 'planning' campaign; never blocks, never raises.

        NOTE: this mutates campaign state (mail-tracked) — do not call
        from read-only contexts (computes, constraints, reports).
        """
        self.ensure_one()
        campaign = self.campaign_id
        if campaign.state == 'closed':
            _logger.warning(
                'Deal submission %s plots under CLOSED campaign %s (%s) — '
                'allowed per never-block policy, but the campaign rollup '
                'will keep accruing.', self.id, campaign.id, campaign.name)
            return
        campaign.action_approve()  # filters to 'planning' internally

    def _format_sales_details(self):
        """Render the public Sales Details string from the structured discount
        type + value (+ MSRP). BOGO and Bundle now read their structured spec
        (#93677): the bogo buy/get/pct triple and the bundle_tier_ids table.

        Driven by the existing discount_type Selection rather than a redundant
        new "sales format" field (#94626). discount_type already enumerates the
        formats: Percentage Off / Fixed Amount Off / Set Price / BOGO /
        Clearance / Loyalty Points Multiplier / Bundle Deal.
        """
        self.ensure_one()
        dt = self.discount_type
        v = self.discount_value
        n = (lambda x: f"{x:g}")              # 40.0 -> "40", 12.5 -> "12.5"
        money = (lambda x: f"${x:.0f}" if x == int(x) else f"${x:.2f}")  # $20 / $22.50
        if dt == 'percent' and v:
            return f"{n(v)}% Off"
        if dt == 'fixed' and v:
            return f"{money(v)} Off"
        if dt == 'price' and v:
            return money(v)
        if dt == 'bogo':
            return self._bogo_sales_text() or "BOGO"
        if dt == 'bundle':
            return format_bundle_tiers_text(
                [(t.qty, t.price) for t in self.bundle_tier_ids])
        if dt == 'clearance':
            return f"{n(v)}% Off (Clearance)" if v else "Clearance"
        if dt == 'points_multiplier' and v:
            return f"{n(v)}x Points"
        return ''  # no value -> keep free text

    sales_details_autogen = fields.Char(
        string='Last Auto-generated Sales Details',
        help='Internal: last value _onchange_autofill_sales_details wrote, so '
             'structured-field edits can refresh the text without clobbering '
             'a manual override.',
    )

    @api.onchange('discount_type', 'discount_value', 'bogo_variant',
                  'bogo_buy_qty', 'bogo_get_qty', 'bogo_get_pct',
                  'bundle_tier_ids')
    def _onchange_autofill_sales_details(self):
        """Auto-generate Sales Details from the discount type when it's still
        blank OR still equal to the previous auto-fill, so submitters stop
        free-typing inconsistent pricing strings (#94626 / #93677). Never
        clobbers a custom edit."""
        generated = self._format_sales_details()
        if not generated:
            return
        if not self.sales_details or self.sales_details == self.sales_details_autogen:
            self.sales_details = generated
            self.sales_details_autogen = generated

    @api.depends('brand_id', 'brand_ids')
    def _compute_available_category_ids(self):
        """Distinct categories the selected brand(s) have in stock right now.

        x_quantity_available on product.template is the Dutchie-synced
        on-hand quantity (refreshed continuously by the inventory sync), so
        a plain aggregate here is cheap and always current — no inventory-
        service HTTP round-trip needed. sudo: submitters reviewing deals
        don't necessarily hold product read rights, and this exposes only
        category names.
        """
        Product = self.env['product.template'].sudo()
        for sub in self:
            brands = sub.brand_ids | sub.brand_id
            if not brands:
                sub.available_category_ids = False
                continue
            groups = Product._read_group(
                [('brand_id', 'in', brands.ids),
                 ('x_quantity_available', '>', 0),
                 ('categ_id', '!=', False)],
                groupby=['categ_id'],
                aggregates=[],
            )
            sub.available_category_ids = [(6, 0, [g[0].id for g in groups])]

    def _all_picked_categories(self):
        """Union of the multi-select and the legacy single pick, multi first.
        Mirrors the brand_ids|brand_id union used everywhere for brands."""
        self.ensure_one()
        return self.product_category_ids | self.product_category_id

    @api.depends('brand_id', 'brand_ids', 'product_category_id',
                 'product_category_ids', 'market_id', 'store_ids')
    def _compute_available_product_ids(self):
        """Products matching every facet, for the Specific Products domain.

        Searched on product.product (not dotted template domains) so the
        SAME variant must carry both the store location and the stock —
        dotted m2o-path conditions each match against any variant
        independently, which would pass a product stocked only outside the
        market. Store scope = requested store_ids when set, else the
        market's stores; no stores resolved -> stock anywhere. sudo for the
        same reason as _compute_available_category_ids.
        """
        Variant = self.env['product.product'].sudo()
        for sub in self:
            brands = sub.brand_ids | sub.brand_id
            if not brands:
                sub.available_product_ids = False
                continue
            domain = [
                ('product_tmpl_id.brand_id', 'in', brands.ids),
                ('x_quantity_available', '>', 0),
            ]
            cats = sub._all_picked_categories()
            if cats:
                domain.append(('product_tmpl_id.categ_id', 'in', cats.ids))
            stores = sub.store_ids or (
                sub.market_id and sub.market_id.store_ids
                or self.env['res.company'].browse([])
            )
            uuids = [
                s.dutchie_store_id for s in stores
                if getattr(s, 'dutchie_store_id', False)
            ]
            if uuids:
                domain.append(('x_dutchie_location_id', 'in', uuids))
            variants = Variant.search(domain)
            sub.available_product_ids = [
                (6, 0, variants.product_tmpl_id.ids)
            ]

    def _resolve_publish_match_count(self):
        """Count the IN-STOCK products this deal's restriction set actually
        resolves to at the target stores — i.e. what the Dutchie discount
        will match in the storefront cache.

        Mirrors the published restriction precedence: explicit Specific
        Products are authoritative; otherwise brand(s) ∩ picked categor(ies).
        Scoped to the requested stores (else the market's stores) and to
        on-hand stock (x_quantity_available > 0, the same Dutchie-synced
        signal the FE inventory cache is built from). Searched on
        product.product so one variant carries both the store and the stock.

        A count of 0 means the deal would be invisible on the storefront and
        apply to nothing at the register — the convert gate refuses it.
        Returns -1 when there is no product-narrowing restriction at all
        (no brand / no explicit products): that 'store-wide' case is caught
        separately by the Dutchie publish builder, so the gate skips it.
        """
        self.ensure_one()
        Variant = self.env['product.product'].sudo()
        brands = self.brand_ids | self.brand_id
        explicit = self.product_ids.filtered(
            lambda p: not brands or p.brand_id in brands
        )
        domain = [('x_quantity_available', '>', 0)]
        if explicit:
            domain.append(('product_tmpl_id', 'in', explicit.ids))
        elif brands:
            domain.append(('product_tmpl_id.brand_id', 'in', brands.ids))
            cats = self._all_picked_categories()
            if cats:
                domain.append(('product_tmpl_id.categ_id', 'in', cats.ids))
        else:
            return -1  # no brand & no explicit products → store-wide, not gated here
        stores = self.store_ids or (
            self.market_id and self.market_id.store_ids
            or self.env['res.company'].browse([])
        )
        uuids = [s.dutchie_store_id for s in stores
                 if getattr(s, 'dutchie_store_id', False)]
        if uuids:
            domain.append(('x_dutchie_location_id', 'in', uuids))
        variants = Variant.search(domain)
        return len(set(variants.product_tmpl_id.ids))

    @api.depends('brand_id', 'brand_ids', 'product_ids',
                 'product_category_id', 'product_category_ids',
                 'market_id', 'store_ids')
    def _compute_publish_match_count(self):
        """Surfaced on the form so the approver sees how many in-stock
        products the deal will hit BEFORE converting (the resolve-and-gate
        number)."""
        for sub in self:
            sub.publish_match_count = sub._resolve_publish_match_count()

    def _ptl_category_bucket(self):
        """Map this submission's category picks / free text onto the master
        bucket Selection that mint.ptl.deal.product_category accepts.

        The PTL field is a Selection (Flower / Pre-Rolls / Vapes / ...), so
        raw Dutchie leaf names ('Infused Prerolls') or comma-joined text
        would raise ValueError on conversion. Exact bucket text passes
        through; otherwise each picked name is reverse-matched against
        MASTER_CATEGORY_PATTERNS fragments and the first bucket wins
        (multi-category nuance is preserved in product_category_legacy and
        the published Dutchie restriction, not the PTL display bucket).
        Returns False when nothing matches."""
        self.ensure_one()
        text = (self.product_category or '').strip()
        if text in MASTER_CATEGORY_PATTERNS:
            return text
        names = [c.name for c in self._all_picked_categories()]
        if not names and text:
            names = [t.strip() for t in text.split(',') if t.strip()]
        for name in names:
            low = name.lower()
            for bucket, patterns in MASTER_CATEGORY_PATTERNS.items():
                if any(p.lower() in low for p in patterns):
                    return bucket
        return False

    @api.onchange('product_category_id', 'product_category_ids')
    def _onchange_product_category_fill_text(self):
        """Mirror the dropdown picks into the legacy free-text field that all
        downstream consumers (PTL conversion, stock-check wizard, Dutchie
        publish) read. Leaf names only ('Infused Prerolls'), not the
        'Cannabis / ...' path — that's what Dutchie item categories match.
        Multiple picks join comma-separated. Also keeps the back-compat
        single product_category_id aligned to the first multi pick."""
        cats = self._all_picked_categories()
        if cats:
            self.product_category = ', '.join(cats.mapped('name'))
        if self.product_category_ids and (
                not self.product_category_id
                or self.product_category_id not in self.product_category_ids):
            self.product_category_id = self.product_category_ids[:1]

    @api.onchange('brand_id', 'brand_ids')
    def _onchange_brands_reset_category(self):
        """Drop stale category picks when the brand set changes and the
        new brand(s) no longer stock them. The free-text field is left
        alone — it may carry an intentional manual value."""
        avail = set(self.available_category_ids._origin.ids)
        kept = self.product_category_ids._origin.filtered(lambda c: c.id in avail)
        if len(kept) != len(self.product_category_ids):
            self.product_category_ids = [(6, 0, kept.ids)]
        current = self.product_category_id._origin.id
        if current and current not in avail:
            self.product_category_id = kept[:1] if kept else False

    @api.model
    def _mirror_category_text(self, vals):
        """Non-form writes (RPC, imports, server actions) that set the
        dropdowns without the text get the same mirroring the onchange does
        in the form view."""
        if vals.get('product_category'):
            return
        Category = self.env['product.category']
        names = []
        if vals.get('product_category_ids'):
            # Resolve ids out of standard m2m command tuples ((6,0,ids)/(4,id))
            ids = []
            for cmd in vals['product_category_ids']:
                if isinstance(cmd, (list, tuple)):
                    if cmd[0] == 6:
                        ids.extend(cmd[2])
                    elif cmd[0] == 4:
                        ids.append(cmd[1])
                elif isinstance(cmd, int):
                    ids.append(cmd)
            names = Category.browse(ids).exists().mapped('name')
        elif vals.get('product_category_id'):
            cat = Category.browse(vals['product_category_id'])
            if cat.exists():
                names = [cat.name]
        if names:
            vals['product_category'] = ', '.join(names)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            self._mirror_category_text(vals)
        return super().create(vals_list)

    def write(self, vals):
        self._mirror_category_text(vals)
        return super().write(vals)

    @api.onchange('market_id')
    def _onchange_market_fill_stores(self):
        """Default Requested Stores to every live dispensary in the chosen
        market (state). Picking a market selects all its stores; clearing it
        clears the selection. Mirrors mint.region.store_count's live filter
        (is_dispensary & is_active). The user can still hand-edit afterward —
        the next market change re-fills."""
        if not self.market_id:
            self.store_ids = [(5, 0, 0)]
            return
        stores = self.market_id.store_ids.filtered(
            lambda c: getattr(c, 'is_dispensary', False) and getattr(c, 'is_active', True)
        )
        self.store_ids = [(6, 0, stores.ids)]

    def action_convert_to_deal(self):
        """Create a mint.ptl.deal from this approved submission."""
        self.ensure_one()
        if self.deal_id:
            raise UserError("This submission has already been converted to a deal.")
        if self.state not in ('approved',):
            raise UserError("Only approved submissions can be converted to deals.")

        # Resolve-and-gate: refuse to convert a deal whose brand/category/
        # product selection matches NO in-stock product at the target stores.
        # Such a deal publishes to Dutchie but is invisible on the storefront
        # and discounts nothing at the register — the failure mode is silent
        # today. -1 (no product-narrowing restriction) is the store-wide case,
        # which the Dutchie publish builder refuses separately. Override with
        # context force_empty_deal=True for a deliberate ahead-of-stock deal.
        if not self.env.context.get('force_empty_deal'):
            match_count = self._resolve_publish_match_count()
            if match_count == 0:
                raise UserError(
                    "This deal's selection (brand / In-Stock Categories / "
                    "Specific Products) matches no in-stock products at the "
                    "target stores, so it would be invisible on the storefront "
                    "and apply to nothing at the register.\n\n"
                    "Fix the Inclusions or check stock for the chosen "
                    "brand(s) and categories in this market. To convert anyway "
                    "(e.g. an ahead-of-restock deal), use Convert (Force)."
                )

        self._autoapprove_campaign()

        # Multi-brand (#93635): every selected brand rides through. The
        # union keeps single-brand submissions working unchanged.
        all_brands = (self.brand_ids | self.brand_id) if self.brand_id else self.brand_ids

        # Drop any product picks whose brand no longer matches the
        # submission's brands (e.g. user changed brands after picking).
        # Silent drop is fine — the deal-form's explicit picker is also
        # brand-scoped, so a stale pick would be invisible anyway.
        explicit_products = self.product_ids.filtered(
            lambda p: p.brand_id in all_brands
        ) if all_brands else self.product_ids.browse([])

        deal = self.env['mint.ptl.deal'].create({
            'name': self.name,
            'brand_id': (self.brand_id or all_brands[:1]).id if all_brands else False,
            'brand_ids': [(6, 0, all_brands.ids)] if all_brands else False,
            'product_category': self._ptl_category_bucket(),
            'product_category_legacy': self.product_category or False,
            'discount_type': self.discount_type,
            'discount_value': self.discount_value,
            'original_price': self.original_price,
            'bogo_variant': self.bogo_variant,
            'bogo_buy_qty': self.bogo_buy_qty,
            'bogo_get_qty': self.bogo_get_qty,
            'bogo_get_pct': self.bogo_get_pct,
            'bundle_tier_ids': [
                (0, 0, {'sequence': t.sequence, 'qty': t.qty, 'price': t.price})
                for t in self.bundle_tier_ids
            ],
            'sales_details': self.sales_details,
            'details_exclusions': self.details_exclusions,
            'store_ids': [(6, 0, self.store_ids.ids)] if self.store_ids else False,
            'market_id': self.market_id.id if self.market_id else False,
            'state': 'approved',
            'submitted_by': self.create_uid.id,
            'submitted_at': self.create_date,
            'vendor_funding_amount': self.vendor_funding_amount,
            'vendor_funding_percent': self.vendor_funding_percent,
            'campaign_id': self.campaign_id.id if self.campaign_id else False,
            'explicit_product_ids': [(6, 0, explicit_products.ids)] if explicit_products else False,
            'excluded_brand_ids': [(6, 0, self.excluded_brand_ids.ids)] if self.excluded_brand_ids else False,
            'excluded_skus': self.excluded_skus or False,
        })
        self.write({
            'state': 'scheduled',
            'deal_id': deal.id,
        })

        # Replay structured plot windows onto the new deal's day_ids. When the
        # submission has no structured windows (legacy / public-form rows that
        # only set preferred_start/end), synthesise a contiguous window from
        # those preferred dates so the deal still plots instead of converting to
        # a zero-day deal that needs manual calendar work (#93723 AC03).
        plotted_count = 0
        window_count = 0
        dates = []
        if self.window_ids:
            dates = self.window_ids.all_dates()
            window_count = len(self.window_ids)
        elif self.preferred_start_date or self.preferred_end_date:
            start = self.preferred_start_date or self.preferred_end_date
            end = self.preferred_end_date or self.preferred_start_date
            if end < start:
                start, end = end, start
            day = start
            while day <= end:
                dates.append(day.isoformat())
                day += timedelta(days=1)
            window_count = 1

        # market_id is required to plot; without it we leave the deal unplotted
        # rather than silently picking a market (action_plot_windows would raise).
        if dates and self.market_id:
            day_ids = deal.action_plot_windows(dates, market_id=self.market_id.id)
            plotted_count = len(day_ids)

        body = f"Scheduled — created PTL Deal: {deal.name} (id={deal.id})"
        if plotted_count:
            body += (
                f" — plotted {plotted_count} day(s) across "
                f"{window_count} window(s)."
            )
        self.message_post(body=body, message_type='comment')
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'mint.ptl.deal',
            'res_id': deal.id,
            'view_mode': 'form',
        }

    def action_convert_to_deal_force(self):
        """Convert past the empty-resolution gate — for a deliberate
        ahead-of-restock deal whose products aren't in stock yet."""
        self.ensure_one()
        return self.with_context(force_empty_deal=True).action_convert_to_deal()

    # ─── Run-end lifecycle: Scheduled -> Final Review -> Expired ──────────

    def action_to_final_review(self):
        """Manually move a scheduled deal into Final Review (closeout)."""
        self.filtered(lambda s: s.state == 'scheduled').write({
            'state': 'final_review',
        })

    def action_expire(self):
        """Sign off a deal after final review (or end a scheduled one early)."""
        self.filtered(lambda s: s.state in ('scheduled', 'final_review')).write({
            'state': 'expired',
        })

    @api.model
    def _cron_advance_lifecycle(self):
        """Daily: move Scheduled deals whose run window has ended into Final
        Review so a human signs off the closeout. Expiry stays manual — Final
        Review is a deliberate human gate, not an automatic archive."""
        today = fields.Date.context_today(self)
        due = self.search([
            ('state', '=', 'scheduled'),
            ('run_end_date', '!=', False),
            ('run_end_date', '<', today),
        ])
        if not due:
            return
        # Advance state first so the closeout still happens even if chatter
        # crashes — message_post can blow up on a rogue automation
        # (TypeError: unhashable list), same guard dutchie_publish uses.
        due.write({'state': 'final_review'})
        for sub in due:
            try:
                sub.message_post(
                    body="Run window ended — moved to Final Review for closeout.",
                    message_type='comment',
                )
            except Exception:
                _logger.exception(
                    "Lifecycle chatter post failed for submission %s", sub.id)
