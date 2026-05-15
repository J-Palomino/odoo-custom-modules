from odoo import api, fields, models


class BrandCalendarEntry(models.Model):
    _name = 'mint.brand.calendar.entry'
    _description = 'Brand Calendar Entry — Scheduled brand promotion slot'
    _inherit = ['mail.thread']
    _order = 'date, brand_id'

    # ── Identity ──────────────────────────────────────────────────────────

    brand_id = fields.Many2one(
        'mint.brand',
        string='Brand',
        required=True,
        index=True,
        tracking=True,
    )
    date = fields.Date(
        string='Date',
        required=True,
        index=True,
        tracking=True,
    )
    market_id = fields.Many2one(
        'mint.region',
        string='Market',
        required=True,
        index=True,
        tracking=True,
    )
    campaign_id = fields.Many2one(
        'mint.national.promo',
        string='Campaign',
        ondelete='set null',
        index=True,
        tracking=True,
    )

    # ── SKU / product targeting ───────────────────────────────────────────

    sku_or_category = fields.Char(
        string='SKU / Category',
        tracking=True,
        help='Raw column header from the National Promo XLSX (e.g. "LR Cannons Prerolls", "1g AIO"). '
             'Free-text by design; resolves async to product_template_id or product_category_id.',
    )
    product_template_id = fields.Many2one(
        'product.template',
        string='Product Template',
        index=True,
        help='Optional precise product link, once sku_or_category is resolved.',
    )
    product_category_id = fields.Many2one(
        'product.category',
        string='Product Category',
        index=True,
        help='Alternative when the header maps to a category rather than a single SKU.',
    )

    # ── Promo math ────────────────────────────────────────────────────────

    promo_text = fields.Char(
        string='Promo Text',
        tracking=True,
        help='Raw XLSX cell content (e.g. "40% off", "2 for $59").',
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
        tracking=True,
    )
    discount_value = fields.Float(string='Discount Value', tracking=True)
    original_price = fields.Float(string='Original / MSRP Price', tracking=True)

    # ── Vendor funding (per-entry override; defaults inherited from campaign) ──

    vendor_funding_amount = fields.Monetary(
        string='Vendor Funding (Per Entry)',
        currency_field='currency_id',
        tracking=True,
    )
    vendor_funding_percent = fields.Float(string='Vendor Funding %', tracking=True)
    currency_id = fields.Many2one(
        'res.currency',
        string='Currency',
        default=lambda self: self.env.company.currency_id,
    )

    # ── Scheduling ────────────────────────────────────────────────────────

    start_time = fields.Float(
        string='Start Time (hour)',
        default=0.0,
        help='Hour of day (24h float) when this entry becomes active. 0/0 = all day.',
    )
    end_time = fields.Float(
        string='End Time (hour)',
        default=0.0,
        help='Hour of day (24h float) when this entry stops being active. 0/0 = all day.',
    )

    # ── Linked PTL artifacts (existing) ──────────────────────────────────

    deal_id = fields.Many2one(
        'mint.ptl.deal',
        string='PTL Deal',
        help='The deal template linked to this calendar slot',
    )
    submission_id = fields.Many2one(
        'mint.deal.submission',
        string='Source Submission',
        help='The vendor submission this entry originated from',
    )
    ptl_day_id = fields.Many2one(
        'mint.ptl.day',
        string='PTL Day',
        help='The PTL day this entry was added to',
        readonly=True,
    )
    slot_type = fields.Selection(
        selection=[
            ('featured', 'Featured'),
            ('standard', 'Standard'),
            ('edlp', 'EDLP'),
        ],
        string='Slot Type',
        default='standard',
    )

    # ── State machine ────────────────────────────────────────────────────

    state = fields.Selection(
        selection=[
            ('tentative', 'Tentative'),
            ('confirmed', 'Confirmed'),
            ('approved', 'Approved'),
            ('published', 'Published'),
        ],
        string='Status',
        default='tentative',
        tracking=True,
    )
    is_published = fields.Boolean(
        string='Published',
        default=False,
        tracking=True,
        help='Marketing flips this when the entry is greenlit for the daily-deals carousel.',
    )

    notes = fields.Text(string='Notes')

    _sql_constraints = [
        ('brand_date_market_sku_uniq',
         'unique(brand_id, date, market_id, sku_or_category, campaign_id)',
         'Only one entry per brand × date × market × SKU/category × campaign.'),
    ]

    # ── State transitions ────────────────────────────────────────────────

    def action_confirm(self):
        self.filtered(lambda e: e.state == 'tentative').write({'state': 'confirmed'})

    def action_approve(self):
        self.filtered(lambda e: e.state in ('tentative', 'confirmed')).write({'state': 'approved'})

    def action_publish(self):
        """Marketing publish — flips is_published, advances state."""
        self.filtered(lambda e: e.state in ('confirmed', 'approved')).write({
            'state': 'published',
            'is_published': True,
        })

    def action_unpublish(self):
        self.filtered(lambda e: e.state == 'published').write({
            'state': 'approved',
            'is_published': False,
        })

    def action_add_to_ptl(self):
        """Create or link to a PTL Day and attach the deal."""
        PtlDay = self.env['mint.ptl.day']
        for entry in self.filtered(lambda e: e.deal_id and e.state in ('tentative', 'confirmed', 'approved')):
            day = PtlDay.search([
                ('date', '=', entry.date),
                ('market_id', '=', entry.market_id.id),
            ], limit=1)
            if not day:
                day = PtlDay.create({
                    'date': entry.date,
                    'market_id': entry.market_id.id,
                })
            day.write({'deal_ids': [(4, entry.deal_id.id)]})
            entry.write({
                'ptl_day_id': day.id,
                'state': 'confirmed' if entry.state == 'tentative' else entry.state,
            })
