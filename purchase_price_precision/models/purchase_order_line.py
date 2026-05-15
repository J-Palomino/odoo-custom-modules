# -*- coding: utf-8 -*-
from odoo import api, fields, models


class PurchaseOrderLine(models.Model):
    _inherit = "purchase.order.line"

    # Packaging details
    x_product_category = fields.Char(string="Product Category")
    x_product_type = fields.Char(string="Product Type")
    x_brand = fields.Char(string="Brand")
    x_size = fields.Char(string="Size")

    # Cost breakdown
    x_additional_costs = fields.Float(string="Additional Costs")
    x_processing_fee = fields.Float(string="Processing Fee")
    x_shipping_cost = fields.Float(string="Shipping Cost")
    x_cost_per_unit_incl = fields.Float(
        string="Cost/Unit (incl fees)",
        compute="_compute_cost_per_unit_incl",
        store=True,
    )

    @api.depends('price_unit', 'x_shipping_cost', 'x_additional_costs', 'x_processing_fee', 'product_qty')
    def _compute_cost_per_unit_incl(self):
        for line in self:
            qty = line.product_qty or 1
            fees = (line.x_shipping_cost or 0) + (line.x_additional_costs or 0) + (line.x_processing_fee or 0)
            line.x_cost_per_unit_incl = line.price_unit + (fees / qty)
