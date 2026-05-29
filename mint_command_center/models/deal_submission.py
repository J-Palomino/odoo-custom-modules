from datetime import date as _date

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError


class DealSubmission(models.Model):
    _name = 'mint.deal.submission'
    _description = 'Vendor Deal Submission'
    _inherit = ['mail.thread', 'mail.activity.mixin']
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
        help='Link to the brand record',
    )

    # --- Vendor funding terms ---
    vendor_funding_amount = fields.Monetary(
        string='Vendor Funding Amount',
        currency_field='currency_id',
        tracking=True,
    )
    vendor_funding_percent = fields.Float(string='Vendor Funding %', tracking=True)
    vendor_funding_terms = fields.Text(string='Funding Terms')
    currency_id = fields.Many2one(
        'res.currency',
        string='Currency',
        default=lambda self: self.env.company.currency_id,
    )

    # --- Deal details ---
    name = fields.Char(string='Deal Name', required=True, tracking=True)
    product_category = fields.Char(string='Product Category')
    product_ids = fields.Many2many(
        'product.template',
        'mint_deal_submission_product_rel',
        'submission_id',
        'product_id',
        string='Products',
        help='Specific products this deal applies to (filtered by brand × category).',
    )
    weight_value = fields.Float(
        string='Weight',
        help='Numeric weight/count of the product (e.g. 3.5 for an eighth).',
    )
    weight_unit = fields.Selection(
        selection=[
            ('g', 'g'),
            ('mg', 'mg'),
            ('oz', 'oz'),
            ('ct', 'ct'),
        ],
        string='Unit',
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
    discount_value = fields.Float(string='Discount Value')
    original_price = fields.Float(string='Original / MSRP Price')
    sales_details = fields.Text(
        string='Sales Details',
        help='Formatted pricing text — how this deal should be displayed',
    )
    inclusions = fields.Text(
        string='Inclusions',
        help='What this deal applies to (SKUs, strains, sizes, etc.).',
    )
    details_exclusions = fields.Text(
        string='Exclusions',
        help='What this deal does NOT apply to (limits, conditions, exclusions).',
    )

    # --- Targeting ---
    market_ids = fields.Many2many(
        'mint.region',
        'mint_deal_submission_market_rel',
        'submission_id',
        'market_id',
        string='Markets',
        tracking=True,
        help='Regions this deal targets. Use the public form to pick multiple.',
    )
    market_id = fields.Many2one(
        'mint.region',
        string='Primary Market',
        compute='_compute_primary_market',
        store=True,
        readonly=False,
        tracking=True,
        help='First of market_ids — kept for downstream compatibility '
             '(action_approve, action_convert_to_deal, mint.national.promo.get_or_create).',
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
        help='Deprecated free-text field. Use plot_date_ids for structured day picks; '
             'kept for one release to avoid breaking existing submissions.',
    )
    plot_date_ids = fields.One2many(
        'mint.deal.submission.day',
        'submission_id',
        string='Plot Dates',
        help='Specific calendar days the vendor wants this deal to run. '
             'Supports non-contiguous selection.',
    )

    # --- Holiday / Special Event ---
    is_holiday = fields.Boolean(
        string='Special Event / Holiday',
        default=False,
        tracking=True,
        help='Marks this deal as a Holiday / Special Event submission '
             '(unlocks Event Name + Promo Units sub-form on the public form).',
    )
    event_name = fields.Char(
        string='Event Name',
        tracking=True,
        help='Required when is_holiday=True. Surfaces in the PTL Category column downstream.',
    )

    # --- Promo Units (internal-only, only shown when is_holiday=True) ---
    promo_units_enabled = fields.Boolean(
        string='Provide Promo Units',
        default=False,
        help='Vendor opts in to providing doorbuster giveaway units '
             '(only applicable for Holiday / Special Event deals). Internal-only — '
             'never displayed on the public PTL.',
    )
    promo_units_product = fields.Char(
        string='Promo Units — Product',
        help='Product the vendor will send for doorbuster giveaways.',
    )
    promo_units_quantity = fields.Integer(
        string='Promo Units — Quantity',
        help='How many promo units the vendor will send.',
    )
    promo_units_delivery_date = fields.Date(
        string='Promo Units — Estimated Delivery',
        help='Rough delivery target; intake team coordinates the actual drop.',
    )

    # --- State machine ---
    state = fields.Selection(
        selection=[
            ('new', 'New'),
            ('under_review', 'Under Review'),
            ('approved', 'Approved'),
            ('rejected', 'Rejected'),
            ('converted', 'Converted to Deal'),
        ],
        string='Status',
        default='new',
        tracking=True,
    )

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

    # --- Computes & constraints ---

    @api.depends('market_ids')
    def _compute_primary_market(self):
        for rec in self:
            # Fall back to the existing market_id value if market_ids is empty —
            # protects legacy rows during the additive migration window.
            if rec.market_ids:
                rec.market_id = rec.market_ids[:1]
            elif not rec.market_id:
                rec.market_id = False

    @api.constrains('is_holiday', 'event_name')
    def _check_event_name_when_holiday(self):
        for rec in self:
            if rec.is_holiday and not (rec.event_name and rec.event_name.strip()):
                raise ValidationError(
                    "Event Name is required when Special Event / Holiday is enabled."
                )

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
            if sub.campaign_id or not sub.brand_id or not sub.market_id:
                continue
            target_year = sub.preferred_start_date.year if sub.preferred_start_date else year
            campaign = Campaign.get_or_create(
                brand_id=sub.brand_id.id,
                market_id=sub.market_id.id,
                year=target_year,
                crm_lead_id=sub.crm_lead_id.id if sub.crm_lead_id else False,
            )
            if campaign:
                sub.campaign_id = campaign.id

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

    # ─── Plot gate ───────────────────────────────────────────────────────
    #
    # Conversion to a mint.ptl.deal requires the parent campaign
    # (mint.national.promo for this brand × market × year) to be approved
    # — i.e. the brand × market is "blessed to plot" per Famous's approved-
    # offers concept. The gate is bypassable via the Force-Approve wizard,
    # which sets `force_override=True` in context and writes a reason to
    # chatter on both the submission and the campaign.

    PLOTTABLE_CAMPAIGN_STATES = ('approved', 'active')

    def _check_plot_gate(self):
        """Raise UserError if the submission's campaign isn't approved,
        unless force_override is set in context."""
        self.ensure_one()
        if self.env.context.get('force_override'):
            return
        campaign = self.campaign_id
        if not campaign:
            raise UserError(
                "No National Promo Campaign linked to this submission yet. "
                "Approve the submission first (creates the campaign), then "
                "approve the campaign — or use Force Approve & Plot."
            )
        if campaign.state not in self.PLOTTABLE_CAMPAIGN_STATES:
            raise UserError(
                f"Campaign \"{campaign.name}\" is in state "
                f"\"{campaign.state}\" — only "
                f"{', '.join(self.PLOTTABLE_CAMPAIGN_STATES)} campaigns can "
                f"be plotted. Approve the campaign first, or use Force "
                f"Approve & Plot to do both with an audit-logged reason."
            )

    def action_force_approve_and_plot(self):
        """Open the Force Approve & Plot wizard for this submission."""
        self.ensure_one()
        return {
            'name': 'Force Approve & Plot',
            'type': 'ir.actions.act_window',
            'res_model': 'mint.deal.force.approve.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_submission_id': self.id},
        }

    def action_convert_to_deal(self):
        """Create a mint.ptl.deal from this approved submission."""
        self.ensure_one()
        if self.deal_id:
            raise UserError("This submission has already been converted to a deal.")
        if self.state not in ('approved',):
            raise UserError("Only approved submissions can be converted to deals.")

        self._check_plot_gate()

        deal = self.env['mint.ptl.deal'].create({
            'name': self.name,
            'brand_id': self.brand_id.id if self.brand_id else False,
            'product_category': self.product_category,
            'product_ids': [(6, 0, self.product_ids.ids)] if self.product_ids else False,
            'weight_value': self.weight_value or 0.0,
            'weight_unit': self.weight_unit or False,
            'discount_type': self.discount_type,
            'discount_value': self.discount_value,
            'original_price': self.original_price,
            'sales_details': self.sales_details,
            'inclusions': self.inclusions,
            'details_exclusions': self.details_exclusions,
            'store_ids': [(6, 0, self.store_ids.ids)] if self.store_ids else False,
            'market_id': self.market_id.id if self.market_id else False,
            'is_holiday': self.is_holiday,
            'event_name': self.event_name,
            'state': 'approved',
            'submitted_by': self.create_uid.id,
            'submitted_at': self.create_date,
            'vendor_funding_amount': self.vendor_funding_amount,
            'vendor_funding_percent': self.vendor_funding_percent,
            'campaign_id': self.campaign_id.id if self.campaign_id else False,
        })
        self.write({
            'state': 'converted',
            'deal_id': deal.id,
        })
        self.message_post(
            body=f"Converted to PTL Deal: {deal.name} (id={deal.id})",
            message_type='comment',
        )
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'mint.ptl.deal',
            'res_id': deal.id,
            'view_mode': 'form',
        }
