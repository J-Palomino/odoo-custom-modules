from datetime import date as _date

from odoo import api, fields, models
from odoo.exceptions import UserError


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
        compute='_compute_primary_option_fields',
        inverse='_inverse_primary_option_fields',
        store=True,
        readonly=False,
    )
    discount_value = fields.Float(
        string='Discount Value',
        compute='_compute_primary_option_fields',
        inverse='_inverse_primary_option_fields',
        store=True,
        readonly=False,
    )
    option_ids = fields.One2many(
        'mint.deal.submission.option',
        'submission_id',
        string='Deal Options',
        help='Each option becomes its own PTL row after approve/convert '
             '(e.g. Bundle + BOGO on the same product).',
    )
    original_price = fields.Float(string='Original / MSRP Price')
    sales_details = fields.Text(
        string='Sales Details',
        help='Formatted pricing text — how this deal should be displayed',
    )
    details_exclusions = fields.Text(
        string='Details & Exclusions',
        help='Product details, exclusions, and conditions',
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

    # --- Primary-option mirror (same pattern as mint.ptl.deal, Phase 2) ---

    def _primary_option(self):
        # Sort explicitly: same-transaction sequence writes don't re-sort
        # the in-cache o2m until flush.
        self.ensure_one()
        return self.option_ids.sorted(lambda o: (o.sequence, o.id))[:1]

    def _primary_option_vals(self, discount_type, discount_value):
        return {
            'submission_id': self.id,
            'sequence': 10,
            'discount_type': discount_type,
            'discount_value': discount_value or 0.0,
        }

    @api.depends('option_ids', 'option_ids.discount_type',
                 'option_ids.discount_value', 'option_ids.sequence')
    def _compute_primary_option_fields(self):
        for rec in self:
            primary = rec._primary_option()
            rec.discount_type = primary.discount_type if primary else False
            rec.discount_value = primary.discount_value if primary else 0.0

    def _inverse_primary_option_fields(self):
        # Legacy write-path: setting discount_type/value on the submission
        # upserts the first option; clearing discount_type unlinks it so the
        # mirror doesn't snap back on next recompute.
        Option = self.env['mint.deal.submission.option']
        for rec in self:
            primary = rec._primary_option()
            if not rec.discount_type:
                if primary:
                    primary.unlink()
                continue
            if primary:
                primary.write({
                    'discount_type': rec.discount_type,
                    'discount_value': rec.discount_value,
                })
            else:
                Option.create(rec._primary_option_vals(
                    rec.discount_type, rec.discount_value,
                ))

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

        # Copy options across so a multi-option submission converts to a
        # multi-option deal. The mirror compute on ptl.deal refreshes
        # discount_type/discount_value from option[0] automatically, so we
        # don't pass them in create() vals here.
        option_vals = [
            (0, 0, {
                'sequence': opt.sequence,
                'discount_type': opt.discount_type,
                'discount_value': opt.discount_value,
                'sales_details': opt.sales_details or False,
            })
            for opt in self.option_ids.sorted(lambda o: (o.sequence, o.id))
        ]
        deal = self.env['mint.ptl.deal'].create({
            'name': self.name,
            'brand_id': self.brand_id.id if self.brand_id else False,
            'product_category': self.product_category,
            'original_price': self.original_price,
            'sales_details': self.sales_details,
            'details_exclusions': self.details_exclusions,
            'option_ids': option_vals,
            'store_ids': [(6, 0, self.store_ids.ids)] if self.store_ids else False,
            'market_id': self.market_id.id if self.market_id else False,
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
