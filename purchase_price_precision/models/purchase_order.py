# -*- coding: utf-8 -*-
from odoo import api, fields, models


class PurchaseOrder(models.Model):
    _inherit = "purchase.order"

    # MT-82: Secondary PO title (shorthand summary)
    x_po_title = fields.Char(string="PO Title", help="Short summary of PO contents")

    # Priority (replaces star widget with High/Medium/Low)
    x_priority_level = fields.Selection([
        ('low', 'Low'),
        ('medium', 'Medium'),
        ('high', 'High'),
    ], string="Priority", default='low')

    # Unit count (total qty across all lines)
    x_unit_count = fields.Float(
        string="Unit Count",
        compute="_compute_unit_count",
        store=True,
    )

    # Current amount due
    x_current_due = fields.Float(string="Current Due", tracking=True)

    # Order tracking
    x_order_status = fields.Selection([
        ('draft', 'Draft'),
        ('confirmed', 'Confirmed'),
        ('in_production', 'In Production'),
        ('sent', 'Sent'),
        ('shipped', 'Shipped'),
        ('in_transit', 'In Transit'),
        ('received', 'Received'),
        ('billed', 'Billed'),
        ('complete', 'Complete'),
        ('cancelled', 'Cancelled'),
    ], string="Order Status", default='draft', tracking=True)

    x_payment_status = fields.Selection([
        ('deposit_due', 'Deposit Due'),
        ('partial_paid', 'Partial Paid'),
        ('paid_in_full', 'Paid in Full'),
    ], string="Payment Status", tracking=True)

    x_shipment_status = fields.Selection([
        ('awaiting_payment', 'Awaiting Payment'),
        ('in_production', 'In Production'),
        ('production_complete', 'Production Complete'),
        ('in_air', 'In Air'),
        ('received', 'Received'),
    ], string="Shipment Status", tracking=True)

    x_state_region = fields.Selection([
        ('Arizona', 'Arizona'),
        ('Florida', 'Florida'),
        ('Illinois', 'Illinois'),
        ('Michigan', 'Michigan'),
        ('Missouri', 'Missouri'),
        ('Nevada', 'Nevada'),
    ], string="State")

    # Financial
    x_invoice_no = fields.Char(string="Invoice No")
    x_shipping_cost_total = fields.Float(string="Total Shipping Cost")
    x_additional_costs_total = fields.Float(string="Total Additional Costs")
    x_total_shipping_cost = fields.Float(string="Total Shipping Cost (alt)")
    x_grand_total = fields.Float(
        string="Grand Total incl. Shipping",
        compute="_compute_grand_total",
        store=True,
    )

    # Shipping
    x_by_sea = fields.Boolean(string="By Sea")
    x_by_air = fields.Boolean(string="By Air")
    x_by_ground = fields.Boolean(string="By Ground")
    x_tracking = fields.Char(string="Tracking")

    # Artwork
    x_artwork_created = fields.Boolean(string="Artwork Created")
    x_artwork_approved = fields.Boolean(string="Artwork Approved")
    x_artwork_submitted = fields.Boolean(string="Artwork Submitted")
    x_artwork_link = fields.Char(string="Artwork Link")
    x_reorder = fields.Boolean(string="Reorder")

    # Vendor & Notes
    x_vendor_contact = fields.Char(string="Vendor Contact")
    x_comment_date = fields.Char(string="Comment Date")
    x_comments = fields.Text(string="Comments")

    # MT-69: Prevent setting status to "received" without a validated receipt
    @api.constrains('x_order_status')
    def _check_received_has_transfer(self):
        for order in self:
            if order.x_order_status == 'received':
                done_pickings = order.picking_ids.filtered(
                    lambda p: p.state == 'done'
                )
                if not done_pickings:
                    raise models.ValidationError(
                        "Cannot set status to 'Received' without a validated "
                        "inventory transfer. Please process the receipt first."
                    )

    @api.depends('order_line.product_qty')
    def _compute_unit_count(self):
        for order in self:
            order.x_unit_count = sum(order.order_line.mapped('product_qty'))

    @api.depends('amount_total', 'x_shipping_cost_total', 'x_additional_costs_total')
    def _compute_grand_total(self):
        for order in self:
            order.x_grand_total = (
                order.amount_total
                + (order.x_shipping_cost_total or 0)
                + (order.x_additional_costs_total or 0)
            )
