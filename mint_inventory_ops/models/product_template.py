from odoo import models, fields


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    x_brand = fields.Char(string='Brand')
    x_category = fields.Char(string='Category')

    inventory_status = fields.Selection([
        ('active', 'Active'),
        ('hold', 'On Hold'),
        ('quarantine', 'Quarantine'),
        ('discontinued', 'Discontinued'),
        ('destroyed', 'Destroyed'),
    ], string='Inventory Status', default='active', tracking=True)

    discontinue_reason = fields.Text(string='Discontinue Reason')
    discontinue_date = fields.Date(string='Discontinued Date')
    last_adjustment_id = fields.Many2one(
        'mint.inventory.adjustment', string='Last Adjustment',
        readonly=True,
    )
