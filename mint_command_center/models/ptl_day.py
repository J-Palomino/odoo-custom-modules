from odoo import api, fields, models


class PtlDay(models.Model):
    _name = 'mint.ptl.day'
    _description = 'PTL Day — Promotional day for a store'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'date desc, company_id'

    name = fields.Char(
        string='Name',
        compute='_compute_name',
        store=True,
        readonly=False,
    )
    date = fields.Date(
        string='Date',
        required=True,
        index=True,
        tracking=True,
    )
    company_id = fields.Many2one(
        'res.company',
        string='Store',
        required=True,
        index=True,
        tracking=True,
    )
    state = fields.Selection(
        selection=[
            ('draft', 'Draft'),
            ('confirmed', 'Confirmed'),
            ('published', 'Published'),
        ],
        string='Status',
        default='draft',
        tracking=True,
    )
    deal_ids = fields.One2many(
        'mint.ptl.deal',
        'ptl_day_id',
        string='Deals',
    )
    deal_count = fields.Integer(
        string='Deal Count',
        compute='_compute_deal_count',
        store=True,
    )
    notes = fields.Text(string='Notes')
    is_florida = fields.Boolean(
        string='Florida Store',
        compute='_compute_is_florida',
    )

    _sql_constraints = [
        ('date_company_uniq', 'unique(date, company_id)',
         'Only one PTL day per store per date is allowed.'),
    ]

    @api.depends('date', 'company_id')
    def _compute_name(self):
        for rec in self:
            if rec.date and rec.company_id:
                rec.name = f"{rec.date} — {rec.company_id.name}"
            elif rec.date:
                rec.name = str(rec.date)
            else:
                rec.name = 'New PTL Day'

    @api.depends('company_id')
    def _compute_is_florida(self):
        for rec in self:
            rec.is_florida = bool(
                rec.company_id
                and rec.company_id.state_id
                and rec.company_id.state_id.code == 'FL'
            )

    @api.depends('deal_ids')
    def _compute_deal_count(self):
        for rec in self:
            rec.deal_count = len(rec.deal_ids)
