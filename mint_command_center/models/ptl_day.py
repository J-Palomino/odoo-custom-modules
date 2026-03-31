from odoo import api, fields, models


class PtlDay(models.Model):
    _name = 'mint.ptl.day'
    _description = 'PTL Day — Daily promotional schedule'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'date desc'

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
    deal_ids = fields.Many2many(
        'mint.ptl.deal',
        'mint_ptl_day_deal_rel',
        'day_id',
        'deal_id',
        string='Deals',
    )
    deal_count = fields.Integer(
        string='Deal Count',
        compute='_compute_deal_count',
        store=True,
    )
    notes = fields.Text(string='Notes')

    _sql_constraints = [
        ('date_uniq', 'unique(date)', 'Only one PTL day per date is allowed.'),
    ]

    @api.depends('date')
    def _compute_name(self):
        for rec in self:
            if rec.date:
                rec.name = str(rec.date)
            else:
                rec.name = 'New PTL Day'

    @api.depends('deal_ids')
    def _compute_deal_count(self):
        for rec in self:
            rec.deal_count = len(rec.deal_ids)
