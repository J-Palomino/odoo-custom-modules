from odoo import models, fields, api


class InventoryAdjustmentLine(models.Model):
    _name = 'mint.inventory.adjustment.line'
    _description = 'Inventory Adjustment Line'
    _order = 'sequence, id'

    adjustment_id = fields.Many2one(
        'mint.inventory.adjustment', string='Adjustment',
        required=True, ondelete='cascade',
    )
    sequence = fields.Integer(default=10)

    product_id = fields.Many2one(
        'product.product', string='Product', required=True,
    )
    lot_id = fields.Many2one('stock.lot', string='Lot / Batch')

    # Quantities
    current_qty = fields.Float(
        string='Current Qty', compute='_compute_current_qty',
        store=True, readonly=True,
    )
    new_qty = fields.Float(
        string='New Qty',
        help='Target quantity (for adjust/restore operations)',
    )
    quantity = fields.Float(
        string='Quantity',
        help='Amount to move/destroy/combine',
    )

    # Locations (for move operations)
    source_location_id = fields.Many2one('stock.location', string='Source Location')
    dest_location_id = fields.Many2one('stock.location', string='Destination Location')

    # Product info (related for display)
    sku = fields.Char(related='product_id.default_code', string='SKU', readonly=True)
    brand = fields.Char(related='product_id.x_brand', string='Brand', readonly=True)
    category = fields.Char(related='product_id.x_category', string='Category', readonly=True)

    # Compliance
    thc = fields.Char(string='THC %')
    cbd = fields.Char(string='CBD %')
    metrc_tag = fields.Char(string='Metrc Tag')

    notes = fields.Text(string='Notes')

    @api.depends('product_id', 'adjustment_id.location_id')
    def _compute_current_qty(self):
        for line in self:
            if line.product_id and line.adjustment_id.location_id:
                quant = self.env['stock.quant'].sudo().search([
                    ('product_id', '=', line.product_id.id),
                    ('location_id', '=', line.adjustment_id.location_id.id),
                ], limit=1)
                line.current_qty = quant.quantity if quant else 0.0
            else:
                line.current_qty = 0.0

    def _to_api_dict(self):
        self.ensure_one()
        return {
            'id': self.id,
            'product_id': self.product_id.id,
            'product_name': self.product_id.name,
            'sku': self.sku or '',
            'brand': self.brand or '',
            'category': self.category or '',
            'current_qty': self.current_qty,
            'new_qty': self.new_qty,
            'quantity': self.quantity,
            'thc': self.thc or '',
            'cbd': self.cbd or '',
            'metrc_tag': self.metrc_tag or '',
            'notes': self.notes or '',
        }
