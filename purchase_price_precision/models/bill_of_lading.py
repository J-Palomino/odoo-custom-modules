# -*- coding: utf-8 -*-
"""
MT-71: Bill of Lading — interactive receiving checklist.

Natalie creates a BOL from a PO. The receiving party opens the same
record in Odoo, fills in actual received quantities, and notes any
discrepancies. Both users see updates in real-time via Odoo chatter.
"""
from odoo import api, fields, models


class BillOfLading(models.Model):
    _name = "purchase.bill.of.lading"
    _description = "Bill of Lading"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "create_date desc"

    name = fields.Char(
        string="BOL Reference",
        required=True,
        copy=False,
        readonly=True,
        default="New",
    )
    purchase_order_id = fields.Many2one(
        "purchase.order",
        string="Purchase Order",
        required=True,
        ondelete="cascade",
    )
    company_id = fields.Many2one(
        related="purchase_order_id.company_id",
        store=True,
    )
    partner_id = fields.Many2one(
        related="purchase_order_id.partner_id",
        string="Vendor",
        store=True,
    )
    date_shipped = fields.Date(string="Ship Date", tracking=True)
    date_expected = fields.Datetime(
        string="Expected Arrival",
        related="purchase_order_id.date_planned",
        readonly=False,
    )
    date_received = fields.Date(string="Date Received", tracking=True)
    state = fields.Selection([
        ("draft", "Draft"),
        ("sent", "Sent"),
        ("in_transit", "In Transit"),
        ("received", "Received"),
        ("verified", "Verified"),
    ], string="Status", default="draft", tracking=True)

    # Shipping info
    carrier = fields.Char(string="Carrier")
    tracking_number = fields.Char(string="Tracking Number")
    shipping_method = fields.Selection([
        ("sea", "By Sea"),
        ("air", "By Air"),
        ("ground", "By Ground"),
    ], string="Shipping Method")

    # Receiver
    received_by = fields.Many2one(
        "res.users",
        string="Received By",
        tracking=True,
    )
    receiver_notes = fields.Text(string="Receiver Notes")

    # Lines
    line_ids = fields.One2many(
        "purchase.bill.of.lading.line",
        "bol_id",
        string="Items",
    )

    # Discrepancy flag (computed)
    has_discrepancy = fields.Boolean(
        string="Has Discrepancies",
        compute="_compute_has_discrepancy",
        store=True,
    )

    @api.depends("line_ids.qty_received", "line_ids.qty_expected")
    def _compute_has_discrepancy(self):
        for bol in self:
            bol.has_discrepancy = any(
                line.qty_received != line.qty_expected
                for line in bol.line_ids
                if line.qty_received > 0
            )

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("name", "New") == "New":
                vals["name"] = self.env["ir.sequence"].next_by_code(
                    "purchase.bill.of.lading"
                ) or "New"
        return super().create(vals_list)

    def action_send(self):
        """Mark BOL as sent and notify followers."""
        self.write({"state": "sent"})

    def action_in_transit(self):
        self.write({"state": "in_transit"})

    def action_receive(self):
        self.write({
            "state": "received",
            "date_received": fields.Date.today(),
            "received_by": self.env.uid,
        })

    def action_verify(self):
        self.write({"state": "verified"})

    def action_create_from_po(self):
        """Create a BOL from a purchase order, copying line items."""
        self.ensure_one()
        po = self.purchase_order_id
        for line in po.order_line:
            self.env["purchase.bill.of.lading.line"].create({
                "bol_id": self.id,
                "po_line_id": line.id,
                "product_id": line.product_id.id,
                "description": line.name,
                "qty_expected": line.product_qty,
                "uom_id": line.product_uom.id,
            })


class BillOfLadingLine(models.Model):
    _name = "purchase.bill.of.lading.line"
    _description = "Bill of Lading Line"
    _order = "sequence, id"

    bol_id = fields.Many2one(
        "purchase.bill.of.lading",
        string="Bill of Lading",
        required=True,
        ondelete="cascade",
    )
    sequence = fields.Integer(default=10)
    po_line_id = fields.Many2one(
        "purchase.order.line",
        string="PO Line",
    )
    product_id = fields.Many2one(
        "product.product",
        string="Product",
    )
    description = fields.Char(string="Description")
    qty_expected = fields.Float(string="Expected Qty")
    qty_received = fields.Float(string="Received Qty")
    uom_id = fields.Many2one("uom.uom", string="Unit")
    discrepancy_note = fields.Char(string="Note")

    is_match = fields.Boolean(
        string="Match",
        compute="_compute_is_match",
        store=True,
    )

    @api.depends("qty_expected", "qty_received")
    def _compute_is_match(self):
        for line in self:
            line.is_match = (
                line.qty_received > 0
                and line.qty_received == line.qty_expected
            )
