from odoo import api, fields, models


class PtlDealOption(models.Model):
    _name = 'mint.ptl.deal.option'
    _inherit = ['mint.discount.option.mixin']
    _description = 'PTL Deal Option — one discount spec on a multi-option deal'
    _order = 'ptl_deal_id, sequence, id'

    # Tell the mixin formatter where to find the host's MSRP.
    _parent_msrp_path = 'ptl_deal_id.original_price'

    ptl_deal_id = fields.Many2one(
        'mint.ptl.deal',
        string='PTL Deal',
        required=True,
        ondelete='cascade',
        index=True,
    )

    # Override sales_details to swap the help text — falls back to the
    # parent ptl.deal's sales_details when blank.
    sales_details = fields.Text(
        string='Sales Details',
        help='Optional per-option override for the customer-facing pricing text. '
             'Falls back to the parent ptl.deal sales_details when blank.',
    )

    @api.depends('discount_type', 'discount_value', 'sales_details',
                 'ptl_deal_id.original_price')
    def _compute_display_text(self):
        # Re-decorate with the parent-MSRP dependency so the mirror
        # refreshes when the deal's original_price changes.
        return super()._compute_display_text()
