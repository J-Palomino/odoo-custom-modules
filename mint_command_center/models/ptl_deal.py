from odoo import api, fields, models


class PtlDeal(models.Model):
    _name = 'mint.ptl.deal'
    _description = 'PTL Deal — Individual deal within a PTL day'
    _order = 'sequence, id'

    name = fields.Char(string='Deal Name', required=True)
    ptl_day_id = fields.Many2one(
        'mint.ptl.day',
        string='PTL Day',
        required=True,
        ondelete='cascade',
        index=True,
    )
    company_id = fields.Many2one(
        related='ptl_day_id.company_id',
        string='Store',
        store=True,
        index=True,
    )
    date = fields.Date(
        related='ptl_day_id.date',
        string='Date',
        store=True,
        index=True,
    )
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
