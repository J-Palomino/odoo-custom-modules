from odoo import api, fields, models


class PtlDeal(models.Model):
    _name = 'mint.ptl.deal'
    _description = 'PTL Deal — Reusable deal template referenced by PTL days'
    _order = 'sequence, id'

    name = fields.Char(string='Deal Name', required=True)
    brand_id = fields.Many2one(
        'res.partner',
        string='Brand',
    )
    product_category = fields.Char(string='Product Category')
    discount_type = fields.Selection(
        selection=[
            ('percent', 'Percentage Off'),
            ('fixed', 'Fixed Amount Off'),
            ('bogo', 'BOGO'),
            ('bundle', 'Bundle Deal'),
            ('price', 'Set Price'),
        ],
        string='Discount Type',
    )
    discount_value = fields.Float(string='Discount Value')
    sales_details = fields.Text(
        string='Sales Details',
        help='Formatted pricing text displayed to customers (PTL Column D)',
    )
    sale_type = fields.Selection(
        selection=[
            ('edlp', 'EDLP'),
            ('weekly_bundle', 'Weekly Bundle'),
            ('special_offer', 'Special Offer'),
            ('bogo', 'BOGO'),
        ],
        string='Sale Type',
        help='PTL Column E — sale classification',
    )
    details_exclusions = fields.Text(
        string='Details & Exclusions',
        help='Product details, exclusions, and conditions (PTL Column C)',
    )
    store_ids = fields.Many2many(
        'res.company',
        'mint_ptl_deal_store_rel',
        'deal_id',
        'company_id',
        string='Available Stores',
        help='Stores where this deal is available. Leave empty for all stores.',
    )
    description = fields.Text(string='Description')
    sequence = fields.Integer(string='Sequence', default=10)
    is_featured = fields.Boolean(string='Featured')
    state = fields.Selection(
        selection=[
            ('pending', 'Pending'),
            ('approved', 'Approved'),
            ('live', 'Live'),
            ('expired', 'Expired'),
        ],
        string='Status',
        default='pending',
    )
    discount_id = fields.Many2one(
        'mint.discount',
        string='Discount Record',
        help='The mint.discount record synced to Redis for this deal.',
        ondelete='set null',
    )
    day_ids = fields.Many2many(
        'mint.ptl.day',
        'mint_ptl_day_deal_rel',
        'deal_id',
        'day_id',
        string='PTL Days',
    )
    day_count = fields.Integer(
        string='Days Active',
        compute='_compute_day_count',
    )

    @api.depends('day_ids')
    def _compute_day_count(self):
        for rec in self:
            rec.day_count = len(rec.day_ids)
