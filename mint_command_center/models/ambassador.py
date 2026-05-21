from odoo import api, fields, models


class MintAmbassador(models.Model):
    """A brand ambassador — external contractor representing a vendor at Mint
    stores, partner events, or competitor scouting. Backed by `res.partner`
    so name/email/phone live in one place and can participate in mail, tags,
    duplicate detection, etc."""

    _name = 'mint.ambassador'
    _description = 'Brand Ambassador'
    _inherit = ['mail.thread']
    _order = 'state, partner_id'

    partner_id = fields.Many2one(
        'res.partner',
        string='Person',
        required=True,
        index=True,
        ondelete='restrict',
        help='res.partner record carrying name, email, phone.',
        tracking=True,
    )
    brand_id = fields.Many2one(
        'mint.brand',
        string='Brand',
        index=True,
        tracking=True,
    )
    region_ids = fields.Many2many(
        'mint.region',
        'mint_ambassador_region_rel',
        'ambassador_id',
        'region_id',
        string='Markets',
    )
    state = fields.Selection(
        selection=[
            ('active', 'Active'),
            ('inactive', 'Inactive'),
        ],
        string='Status',
        default='active',
        required=True,
        tracking=True,
    )
    notes = fields.Text(string='Notes')

    shift_ids = fields.One2many(
        'mint.ambassador.shift',
        'ambassador_id',
        string='Shifts',
    )
    upcoming_shift_count = fields.Integer(
        string='Upcoming Shifts',
        compute='_compute_shift_counts',
    )
    total_shift_count = fields.Integer(
        string='Total Shifts',
        compute='_compute_shift_counts',
    )

    name = fields.Char(
        string='Name',
        related='partner_id.display_name',
        store=True,
        index=True,
    )

    @api.depends('shift_ids', 'shift_ids.shift_date', 'shift_ids.state')
    def _compute_shift_counts(self):
        today = fields.Date.context_today(self)
        for rec in self:
            rec.total_shift_count = len(rec.shift_ids)
            rec.upcoming_shift_count = len(rec.shift_ids.filtered(
                lambda s: s.shift_date and s.shift_date >= today
                and s.state == 'scheduled'
            ))

    def action_view_shifts(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': f'Shifts — {self.name}',
            'res_model': 'mint.ambassador.shift',
            'view_mode': 'calendar,list,form',
            'domain': [('ambassador_id', '=', self.id)],
            'context': {'default_ambassador_id': self.id},
        }
