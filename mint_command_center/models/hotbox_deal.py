from odoo import api, fields, models


class HotboxDeal(models.Model):
    _name = 'mint.hotbox.deal'
    _description = 'Hot Box Deal — Quick promotional deal'
    _inherit = ['mail.thread', 'mint.discount.core.mixin']
    _order = 'date desc, sequence'

    name = fields.Char(string='Deal Name', required=True, tracking=True)
    date = fields.Date(
        string='Date',
        required=True,
        index=True,
        tracking=True,
    )
    company_id = fields.Many2one(
        'res.company',
        string='Store',
        required=True,
        index=True,
        tracking=True,
    )
    brand_id = fields.Many2one(
        'mint.brand',
        string='Brand',
    )
    product_name = fields.Char(string='Product Name')
    product_category = fields.Char(string='Product Category')
    # discount_type / discount_value come from mint.discount.core.mixin.
    # original_price keeps hotbox's own label; deal_price is hotbox-specific.
    original_price = fields.Float(string='Original Price')
    deal_price = fields.Float(string='Deal Price')
    quantity_limit = fields.Integer(string='Quantity Limit')
    sequence = fields.Integer(string='Sequence', default=10)
    state = fields.Selection(
        selection=[
            ('pending', 'Pending'),
            ('approved', 'Approved'),
            ('live', 'Live'),
            ('expired', 'Expired'),
        ],
        string='Status',
        default='pending',
        tracking=True,
    )
    is_florida = fields.Boolean(
        string='Florida Store',
        compute='_compute_is_florida',
    )

    @api.depends('company_id')
    def _compute_is_florida(self):
        for rec in self:
            rec.is_florida = bool(
                rec.company_id
                and rec.company_id.state_id
                and rec.company_id.state_id.code == 'FL'
            )
