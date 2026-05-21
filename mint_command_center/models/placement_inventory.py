from odoo import api, fields, models


class MintPlacementInventory(models.Model):
    """One sellable unit of a placement type — a specific budtender station,
    screen, or e-comm slot. State tracks whether the unit is currently sold."""

    _name = 'mint.placement.inventory'
    _description = 'Placement Inventory — sellable placement unit'
    _inherit = ['mail.thread']
    _order = 'market_id, placement_type_id, identifier, id'

    placement_type_id = fields.Many2one(
        'mint.placement.type',
        string='Placement Type',
        required=True,
        index=True,
        ondelete='restrict',
        tracking=True,
    )
    market_id = fields.Many2one(
        'mint.region',
        string='Market',
        required=True,
        index=True,
        tracking=True,
    )
    company_id = fields.Many2one(
        'res.company',
        string='Store',
        help='Specific store this unit lives in. Leave blank for market-wide '
             'placements (e.g. digital ad slots).',
        index=True,
    )
    identifier = fields.Char(
        string='Identifier',
        help='Local label — e.g. "Screen A", "Counter #2".',
    )
    state = fields.Selection(
        selection=[
            ('available', 'Available'),
            ('booked', 'Booked'),
            ('maintenance', 'Maintenance'),
        ],
        string='Status',
        default='available',
        required=True,
        tracking=True,
    )
    notes = fields.Text(string='Notes')

    category = fields.Selection(
        related='placement_type_id.category',
        string='Category',
        store=True,
    )
    price = fields.Monetary(
        related='placement_type_id.price',
        currency_field='currency_id',
    )
    currency_id = fields.Many2one(
        related='placement_type_id.currency_id',
    )

    booking_ids = fields.One2many(
        'mint.placement.booking',
        'placement_inventory_id',
        string='Bookings',
    )

    @api.model
    def _recompute_state_from_bookings(self, inventory_ids=None):
        """Set state=booked when an active/confirmed booking covers today,
        otherwise available. Skips units already in maintenance."""
        today = fields.Date.context_today(self)
        domain = [('state', '!=', 'maintenance')]
        if inventory_ids:
            domain.append(('id', 'in', list(inventory_ids)))
        units = self.search(domain)
        for unit in units:
            covered = self.env['mint.placement.booking'].search_count([
                ('placement_inventory_id', '=', unit.id),
                ('state', 'in', ('confirmed', 'active')),
                ('start_date', '<=', today),
                ('end_date', '>=', today),
            ])
            unit.state = 'booked' if covered else 'available'
