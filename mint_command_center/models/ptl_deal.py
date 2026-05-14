from odoo import api, fields, models
from odoo.exceptions import ValidationError


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
    )
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
    )
    discount_value = fields.Float(
        string='Discount Value',
        help='For points_multiplier, this is the points multiplier (e.g. 2.0 = 2x points).',
    )
    original_price = fields.Float(
        string='Original / MSRP Price',
        help='Manufacturer suggested retail price — used to compute display text',
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
