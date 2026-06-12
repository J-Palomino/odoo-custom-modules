import logging
from datetime import date as _date

from odoo import api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class DealSubmission(models.Model):
    _name = 'mint.deal.submission'
    _description = 'Vendor Deal Submission'
    _inherit = [
        'mail.thread', 'mail.activity.mixin',
        'mint.discount.core.mixin', 'mint.vendor.funding.mixin',
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
    # discount_type / discount_value / original_price come from
    # mint.discount.core.mixin.
    sales_details = fields.Text(
        string='Sales Details',
        help='Formatted pricing text — how this deal should be displayed',
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
        type + value (+ MSRP). Returns '' for types that need extra structure
        the form doesn't capture yet (Bundle/multi-buy) — those stay free-text.

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
            return "BOGO"
        if dt == 'clearance':
            return f"{n(v)}% Off (Clearance)" if v else "Clearance"
        if dt == 'points_multiplier' and v:
            return f"{n(v)}x Points"
        return ''  # bundle / no value -> keep free text

    @api.onchange('discount_type', 'discount_value')
    def _onchange_autofill_sales_details(self):
        """Auto-generate Sales Details from the discount type when it's still
        blank, so submitters stop free-typing inconsistent pricing strings
        (#94626). Only fills when empty — never clobbers a custom edit, and
        Bundle/multi-buy wording is left to the user."""
        if self.sales_details:
            return
        generated = self._format_sales_details()
        if generated:
            self.sales_details = generated

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
            'product_category': self.product_category,
            'discount_type': self.discount_type,
            'discount_value': self.discount_value,
            'original_price': self.original_price,
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

        # Replay any structured plot windows onto the new deal's day_ids.
        # Falls back to no-op when window_ids is empty (legacy submissions
        # that only set preferred_start/end use the old free-text path).
        plotted_count = 0
        if self.window_ids and self.market_id:
            dates = self.window_ids.all_dates()
            if dates:
                day_ids = deal.action_plot_windows(dates, market_id=self.market_id.id)
                plotted_count = len(day_ids)

        body = f"Scheduled — created PTL Deal: {deal.name} (id={deal.id})"
        if plotted_count:
            body += (
                f" — plotted {plotted_count} day(s) across "
                f"{len(self.window_ids)} window(s)."
            )
        self.message_post(body=body, message_type='comment')
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'mint.ptl.deal',
            'res_id': deal.id,
            'view_mode': 'form',
        }

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
