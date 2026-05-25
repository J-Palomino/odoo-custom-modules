from odoo import _, api, exceptions, fields, models


class MintPlacementBooking(models.Model):
    """A sale of placement inventory to a brand. Replaces the React app's
    `placement_bookings` table — same shape, but `company_ids` is a typed
    many2many to `res.company` instead of a string array, and `partner_id`
    standardises billing on `res.partner` so this can grow into real Odoo
    invoicing later."""

    _name = 'mint.placement.booking'
    _description = 'Placement Booking — sold placement inventory'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'start_date desc, id desc'

    name = fields.Char(
        string='Reference',
        copy=False,
        readonly=True,
        index=True,
        default=lambda self: _('New'),
    )
    placement_type_id = fields.Many2one(
        'mint.placement.type',
        string='Placement Type',
        required=True,
        index=True,
        tracking=True,
    )
    placement_inventory_id = fields.Many2one(
        'mint.placement.inventory',
        string='Pinned Slot',
        index=True,
        help='Specific slot this booking consumes. Leave blank for bulk '
             'bookings that span multiple slots — `company_ids` then '
             'enumerates the participating stores.',
        domain="[('placement_type_id', '=', placement_type_id), ('state', '!=', 'maintenance')]",
    )
    market_id = fields.Many2one(
        'mint.region',
        string='Market',
        required=True,
        index=True,
        tracking=True,
    )
    brand_id = fields.Many2one(
        'mint.brand',
        string='Brand',
        index=True,
        tracking=True,
    )
    partner_id = fields.Many2one(
        'res.partner',
        string='Billing Partner',
        index=True,
        help='Vendor / billing contact for invoicing. Typically the same '
             'partner that backs the brand record.',
    )
    contact_id = fields.Many2one(
        'res.partner',
        string='Operational Contact',
        domain="[('parent_id', '=', partner_id)]",
        help='Day-to-day contact for creative delivery, install, etc.',
    )
    start_date = fields.Date(string='Start Date', required=True, tracking=True)
    end_date = fields.Date(string='End Date', required=True, tracking=True)
    company_ids = fields.Many2many(
        'res.company',
        string='Stores',
        help='Stores this booking runs in. For pinned-slot bookings this is '
             'derived from the inventory unit; for bulk bookings, list every '
             'participating store.',
    )
    quantity = fields.Integer(string='Quantity', default=1)
    total_price = fields.Monetary(
        string='Total Price',
        currency_field='currency_id',
        compute='_compute_total_price',
        store=True,
        readonly=False,
        tracking=True,
    )
    currency_id = fields.Many2one(
        'res.currency',
        default=lambda self: self.env.company.currency_id,
        required=True,
    )
    payment_state = fields.Selection(
        selection=[
            ('pending_invoice', 'Pending Invoice'),
            ('invoiced', 'Invoiced'),
            ('paid', 'Paid'),
        ],
        string='Payment',
        default='pending_invoice',
        required=True,
        tracking=True,
    )
    invoice_number = fields.Char(string='Invoice Number')
    notes = fields.Text(string='Notes')
    state = fields.Selection(
        selection=[
            ('draft', 'Draft'),
            ('confirmed', 'Confirmed'),
            ('active', 'Active'),
            ('completed', 'Completed'),
            ('cancelled', 'Cancelled'),
        ],
        string='Status',
        default='draft',
        required=True,
        tracking=True,
    )

    duration_days = fields.Integer(
        string='Days',
        compute='_compute_duration_days',
    )

    @api.model_create_multi
    def create(self, vals_list):
        seq = self.env['ir.sequence']
        for vals in vals_list:
            if not vals.get('name') or vals['name'] == _('New'):
                vals['name'] = seq.next_by_code('mint.placement.booking') or _('New')
        return super().create(vals_list)

    @api.depends('placement_type_id', 'quantity')
    def _compute_total_price(self):
        for rec in self:
            base = rec.placement_type_id.price or 0.0
            qty = rec.quantity or 1
            rec.total_price = base * qty

    @api.depends('start_date', 'end_date')
    def _compute_duration_days(self):
        for rec in self:
            if rec.start_date and rec.end_date:
                rec.duration_days = (rec.end_date - rec.start_date).days + 1
            else:
                rec.duration_days = 0

    @api.constrains('start_date', 'end_date')
    def _check_dates(self):
        for rec in self:
            if rec.start_date and rec.end_date and rec.end_date < rec.start_date:
                raise exceptions.ValidationError(
                    _('End date must be on or after start date.')
                )

    @api.constrains('placement_inventory_id', 'start_date', 'end_date', 'state')
    def _check_no_overlap_on_pinned_slot(self):
        """Two non-cancelled bookings cannot overlap on the same pinned slot."""
        for rec in self:
            if not rec.placement_inventory_id or rec.state == 'cancelled':
                continue
            clash = self.search([
                ('id', '!=', rec.id),
                ('placement_inventory_id', '=', rec.placement_inventory_id.id),
                ('state', '!=', 'cancelled'),
                ('start_date', '<=', rec.end_date),
                ('end_date', '>=', rec.start_date),
            ], limit=1)
            if clash:
                raise exceptions.ValidationError(
                    _('Slot %(slot)s is already booked %(start)s → %(end)s by %(other)s.') % {
                        'slot': rec.placement_inventory_id.display_name,
                        'start': clash.start_date,
                        'end': clash.end_date,
                        'other': clash.name,
                    }
                )

    def action_confirm(self):
        for rec in self:
            rec.state = 'confirmed'
        self._sync_inventory_state()
        return True

    def action_set_active(self):
        for rec in self:
            rec.state = 'active'
        self._sync_inventory_state()
        return True

    def action_complete(self):
        for rec in self:
            rec.state = 'completed'
        self._sync_inventory_state()
        return True

    def action_cancel(self):
        for rec in self:
            rec.state = 'cancelled'
        self._sync_inventory_state()
        return True

    def action_set_draft(self):
        for rec in self:
            rec.state = 'draft'
        self._sync_inventory_state()
        return True

    def _sync_inventory_state(self):
        inv_ids = self.mapped('placement_inventory_id').ids
        if inv_ids:
            self.env['mint.placement.inventory']._recompute_state_from_bookings(inv_ids)
