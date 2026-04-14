from odoo import api, fields, models


class BrandCalendarEntry(models.Model):
    _name = 'mint.brand.calendar.entry'
    _description = 'Brand Calendar Entry — Scheduled brand promotion slot'
    _inherit = ['mail.thread']
    _order = 'date, brand_id'

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
    state = fields.Selection(
        selection=[
            ('tentative', 'Tentative'),
            ('confirmed', 'Confirmed'),
            ('published', 'Published'),
        ],
        string='Status',
        default='tentative',
        tracking=True,
    )
    notes = fields.Text(string='Notes')

    _sql_constraints = [
        ('brand_date_market_uniq', 'unique(brand_id, date, market_id)',
         'Only one entry per brand per date per market.'),
    ]

    def action_confirm(self):
        self.filtered(lambda e: e.state == 'tentative').write({'state': 'confirmed'})

    def action_add_to_ptl(self):
        """Create or link to a PTL Day and attach the deal."""
        PtlDay = self.env['mint.ptl.day']
        for entry in self.filtered(lambda e: e.deal_id and e.state in ('tentative', 'confirmed')):
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
                'state': 'confirmed',
            })
