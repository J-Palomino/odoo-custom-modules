from odoo import api, fields, models


class DealSubmissionOption(models.Model):
    _name = 'mint.deal.submission.option'
    _inherit = ['mint.discount.option.mixin']
    _description = 'Deal Submission Option — one discount spec on a multi-option submission'
    _order = 'submission_id, sequence, id'

    _parent_msrp_path = 'submission_id.original_price'

    submission_id = fields.Many2one(
        'mint.deal.submission',
        string='Submission',
        required=True,
        ondelete='cascade',
        index=True,
    )

    sales_details = fields.Text(
        string='Sales Details',
        help='Optional per-option override for the customer-facing pricing text. '
             'Falls back to the parent submission sales_details when blank.',
    )

    @api.depends('discount_type', 'discount_value', 'sales_details',
                 'submission_id.original_price')
    def _compute_display_text(self):
        return super()._compute_display_text()
