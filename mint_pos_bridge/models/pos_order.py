# -*- coding: utf-8 -*-
import logging
from odoo import api, fields, models

_logger = logging.getLogger(__name__)

ORDER_STATES = [
    ('lobby', 'Lobby'),
    ('online_orders', 'Online Orders'),
    ('sales_floor', 'Sales Floor / Needs Code'),
    ('processing', 'Processing'),
    ('pickup', 'Pick-Up'),
    ('deli_counter', 'Deli Counter'),
    ('credit_checkout', 'Credit Checkout'),
    ('delivery', 'Delivery'),
    ('ready_delivery', 'Ready For Delivery'),
    ('delivery_progress', 'Delivery In Progress'),
    ('delivery_completed', 'Delivery Completed'),
    ('completed', 'Completed'),
    ('cancelled', 'Cancelled'),
]

ORDER_TYPES = [
    ('pickup', 'Pickup'),
    ('delivery', 'Delivery'),
    ('in_store', 'In-Store'),
]

PAYMENT_METHODS = [
    ('online', 'Online'),
    ('cash', 'Cash'),
    ('debit', 'Debit'),
    ('card', 'Card'),
]


class MintPosOrder(models.Model):
    _name = 'mint.pos.order'
    _description = 'POS Order'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'placed_at desc, id desc'

    name = fields.Char(
        string='Order Number',
        readonly=True,
        copy=False,
        default='New',
    )
    partner_id = fields.Many2one(
        'res.partner',
        string='Customer',
        index=True,
        tracking=True,
    )
    company_id = fields.Many2one(
        'res.company',
        string='Store',
        required=True,
        index=True,
        tracking=True,
        default=lambda self: self.env.company,
    )
    dutchie_checkout_id = fields.Char(
        string='Dutchie Checkout ID',
        index=True,
        copy=False,
    )
    dutchie_receipt_no = fields.Char(
        string='Dutchie Receipt #',
        index=True,
        copy=False,
    )
    state = fields.Selection(
        selection=ORDER_STATES,
        string='Status',
        default='online_orders',
        required=True,
        tracking=True,
        index=True,
    )
    order_type = fields.Selection(
        selection=ORDER_TYPES,
        string='Order Type',
        default='pickup',
        tracking=True,
    )
    payment_method = fields.Selection(
        selection=PAYMENT_METHODS,
        string='Payment Method',
        tracking=True,
    )
    subtotal = fields.Float(string='Subtotal', digits=(12, 2))
    discount_total = fields.Float(string='Discount Total', digits=(12, 2))
    tax_total = fields.Float(string='Tax Total', digits=(12, 2))
    total = fields.Float(string='Total', digits=(12, 2))

    line_ids = fields.One2many(
        'mint.pos.order.line', 'order_id',
        string='Order Lines',
    )
    line_count = fields.Integer(
        string='Item Count',
        compute='_compute_line_count',
        store=True,
    )

    placed_at = fields.Datetime(
        string='Placed At',
        default=fields.Datetime.now,
        index=True,
    )
    confirmed_at = fields.Datetime(string='Confirmed At')
    ready_at = fields.Datetime(string='Ready At')
    completed_at = fields.Datetime(string='Completed At')

    budtender_id = fields.Many2one(
        'res.users',
        string='Budtender',
        tracking=True,
    )
    notes = fields.Text(string='Notes')

    loyalty_points_earned = fields.Integer(string='Points Earned')
    loyalty_points_redeemed = fields.Integer(string='Points Redeemed')

    # Computed: time since placed (for kanban color)
    wait_minutes = fields.Integer(
        string='Wait (min)',
        compute='_compute_wait_minutes',
    )

    _sql_constraints = [
        ('dutchie_checkout_uniq', 'UNIQUE(company_id, dutchie_checkout_id)',
         'Dutchie checkout ID must be unique per store.'),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', 'New') == 'New':
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'mint.pos.order'
                ) or 'New'
        return super().create(vals_list)

    def write(self, vals):
        res = super().write(vals)
        new_state = vals.get('state')
        if not new_state:
            return res

        now = fields.Datetime.now()

        # Set timestamp fields based on state transitions
        ts_map = {
            'confirmed': 'confirmed_at',
            'ready': 'ready_at',
            'completed': 'completed_at',
            'picked_up': 'completed_at',
        }
        ts_field = ts_map.get(new_state)
        if ts_field:
            for order in self:
                if not order[ts_field]:
                    super(MintPosOrder, order).write({ts_field: now})

        # Push notifications for customer-facing state changes
        notification_states = ('confirmed', 'ready', 'cancelled')
        if new_state in notification_states:
            for order in self:
                if order.partner_id:
                    self._send_order_notification(order, new_state)

        # Real-time bus.bus notification for Odoo UI
        for order in self:
            self.env['bus.bus']._sendone(
                f'mint_pos_{order.company_id.id}',
                'order_update',
                {
                    'id': order.id,
                    'name': order.name,
                    'state': new_state,
                },
            )

        return res

    def _send_order_notification(self, order, state):
        """Send push notification to customer on state change."""
        messages = {
            'confirmed': {
                'title': 'Order Received',
                'body': f'{order.company_id.name} confirmed your order {order.name}',
            },
            'ready': {
                'title': 'Your order is ready!',
                'body': f'Order {order.name} is ready for pickup at {order.company_id.name}',
            },
            'cancelled': {
                'title': 'Order Cancelled',
                'body': f'Order {order.name} at {order.company_id.name} has been cancelled',
            },
        }
        msg = messages.get(state)
        if not msg:
            return

        try:
            order_url = f'/orders?ref={order.name}'

            self.env['mint.push.subscription'].sudo().send_to_partner(
                partner_id=order.partner_id.id,
                title=msg['title'],
                body=msg['body'],
                url=order_url,
                actions=[
                    {'action': 'track', 'title': 'Track Order', 'url': order_url},
                ],
            )
            _logger.info(
                'Push [%s] sent for order %s to partner %s',
                state, order.name, order.partner_id.id,
            )
        except Exception:
            _logger.exception(
                'Failed to send push notification for order %s', order.name,
            )

    @api.depends('line_ids')
    def _compute_line_count(self):
        for order in self:
            order.line_count = len(order.line_ids)

    def _compute_wait_minutes(self):
        now = fields.Datetime.now()
        for order in self:
            if order.placed_at and order.state in ('placed', 'confirmed', 'preparing'):
                delta = now - order.placed_at
                order.wait_minutes = int(delta.total_seconds() / 60)
            else:
                order.wait_minutes = 0

    # ── Action buttons for form view ──────────────────────────────────

    def action_confirm(self):
        self.filtered(lambda o: o.state == 'placed').write({'state': 'confirmed'})

    def action_start_preparing(self):
        self.filtered(lambda o: o.state == 'confirmed').write({'state': 'preparing'})

    def action_mark_ready(self):
        self.filtered(lambda o: o.state in ('confirmed', 'preparing')).write({'state': 'ready'})

    def action_mark_picked_up(self):
        self.filtered(lambda o: o.state == 'ready').write({'state': 'picked_up'})

    def action_complete(self):
        self.filtered(lambda o: o.state in ('ready', 'picked_up')).write({'state': 'completed'})

    def action_cancel(self):
        self.filtered(lambda o: o.state not in ('completed', 'cancelled')).write({'state': 'cancelled'})


class MintPosOrderLine(models.Model):
    _name = 'mint.pos.order.line'
    _description = 'POS Order Line'
    _order = 'sequence, id'

    order_id = fields.Many2one(
        'mint.pos.order',
        string='Order',
        required=True,
        ondelete='cascade',
        index=True,
    )
    sequence = fields.Integer(string='Sequence', default=10)

    product_name = fields.Char(string='Product', required=True)
    dutchie_product_id = fields.Char(string='Dutchie Product ID')
    sku = fields.Char(string='SKU')

    quantity = fields.Float(string='Qty', default=1.0)
    unit_price = fields.Float(string='Unit Price', digits=(12, 2))
    discount = fields.Float(string='Discount', digits=(12, 2))
    tax = fields.Float(string='Tax', digits=(12, 2))
    line_total = fields.Float(
        string='Line Total',
        compute='_compute_line_total',
        store=True,
        digits=(12, 2),
    )

    category = fields.Char(string='Category')
    brand = fields.Char(string='Brand')
    strain_type = fields.Char(string='Strain Type')
    weight = fields.Char(string='Weight')

    # Related fields for filtering
    company_id = fields.Many2one(
        related='order_id.company_id',
        string='Store',
        store=True,
        index=True,
    )

    @api.depends('quantity', 'unit_price', 'discount')
    def _compute_line_total(self):
        for line in self:
            line.line_total = (line.quantity * line.unit_price) - line.discount
