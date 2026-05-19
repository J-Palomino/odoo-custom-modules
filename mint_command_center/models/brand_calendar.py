import re

from odoo import api, fields, models


# Extracts cannabis-weight metadata from a free-text title.
# Matches the first occurrence of <number><unit> where unit ∈ {mg, g, oz, pk, ct}.
# Handles: "1g AIO", "3.5g prepack", "1.3g", "100mg", "1000mg", ".5g", "6pk".
# Returns the first match — for entries like "1g AIO and Preroll pack" it yields 1g.
# `g` is checked AFTER `mg` in the alternation so "100mg" parses as mg, not g.
_WEIGHT_RE = re.compile(r'\b(\d*\.?\d+)\s*(mg|g|oz|pk|ct)\b', re.IGNORECASE)


def _parse_weight(*sources):
    """Try each source string in order; return (value:float, unit:str) or (0.0, False)."""
    for s in sources:
        if not s:
            continue
        m = _WEIGHT_RE.search(s)
        if not m:
            continue
        try:
            value = float(m.group(1))
        except (TypeError, ValueError):
            continue
        unit = m.group(2).lower()
        if unit == 'pk':
            unit = 'ct'
        return value, unit
    return 0.0, False


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

    # ── Weight metadata (parsed from sku_or_category / promo_text) ───────
    # Stored compute with readonly=False: auto-derives from the title but can be
    # overridden manually. Useful for filtering/badging deals on the PTL calendar
    # (e.g. show "1g" or "3.5g" next to the deal card).

    weight_value = fields.Float(
        string='Weight',
        compute='_compute_weight',
        store=True,
        readonly=False,
        tracking=True,
        help='Numeric weight/count parsed from the SKU/Category title (e.g. "1g AIO" → 1.0). '
             'Manually editable; clear to auto-recompute from the title.',
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

    @api.depends('sku_or_category', 'promo_text')
    def _compute_weight(self):
        for rec in self:
            # Skip if user has manually set non-default values that don't match either source.
            # The stored-compute + readonly=False pattern recomputes on dependency change;
            # to preserve a manual override, the user should clear sku_or_category last
            # (or just edit the weight field after — Odoo only recomputes when deps change).
            value, unit = _parse_weight(rec.sku_or_category, rec.promo_text)
            rec.weight_value = value
            rec.weight_unit = unit or False

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
