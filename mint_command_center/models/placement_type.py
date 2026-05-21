from odoo import api, fields, models


class MintPlacementType(models.Model):
    """Catalog of paid placement products that brands can purchase — budtender
    stations, dispensary displays, digital screens, e-comm slots, etc. One
    `mint.placement.type` row defines the *offering* (name + price + cadence);
    actual sellable units live in `mint.placement.inventory` and sales in
    `mint.placement.booking`."""

    _name = 'mint.placement.type'
    _description = 'Placement Type — paid placement catalog entry'
    _order = 'category, sequence, name'

    name = fields.Char(string='Name', required=True, tracking=True)
    category = fields.Selection(
        selection=[
            ('budtender_stations', 'Budtender Stations'),
            ('dispensary_displays', 'Dispensary Displays'),
            ('digital', 'Digital'),
            ('ecomm', 'E-Comm'),
            ('other', 'Other'),
        ],
        string='Category',
        required=True,
        default='other',
        tracking=True,
    )
    price = fields.Monetary(string='Price', required=True, currency_field='currency_id')
    currency_id = fields.Many2one(
        'res.currency',
        default=lambda self: self.env.company.currency_id,
        required=True,
    )
    price_unit = fields.Char(
        string='Price Unit',
        default='month',
        help='Cadence the price applies to: month, week, campaign, etc.',
    )
    price_per = fields.Char(
        string='Price Per',
        help='Unit the price multiplies over: store, screen, slot. '
             'Leave blank for a flat price.',
    )
    min_commitment_months = fields.Integer(
        string='Min Commitment (months)',
        default=1,
    )
    notes = fields.Text(string='Notes')
    sequence = fields.Integer(string='Sequence', default=10)
    active = fields.Boolean(string='Active', default=True)

    inventory_count = fields.Integer(
        string='Inventory Slots',
        compute='_compute_counts',
    )
    booking_count = fields.Integer(
        string='Active Bookings',
        compute='_compute_counts',
    )

    _inherit = ['mail.thread']

    @api.depends()
    def _compute_counts(self):
        Inv = self.env['mint.placement.inventory']
        Booking = self.env['mint.placement.booking']
        for rec in self:
            rec.inventory_count = Inv.search_count([('placement_type_id', '=', rec.id)])
            rec.booking_count = Booking.search_count([
                ('placement_type_id', '=', rec.id),
                ('state', 'in', ('confirmed', 'active')),
            ])

    def action_view_inventory(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': f'Inventory — {self.name}',
            'res_model': 'mint.placement.inventory',
            'view_mode': 'list,form',
            'domain': [('placement_type_id', '=', self.id)],
            'context': {'default_placement_type_id': self.id},
        }

    def action_view_bookings(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': f'Bookings — {self.name}',
            'res_model': 'mint.placement.booking',
            'view_mode': 'list,calendar,form',
            'domain': [('placement_type_id', '=', self.id)],
            'context': {'default_placement_type_id': self.id},
        }
