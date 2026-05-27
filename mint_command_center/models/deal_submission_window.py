from odoo import api, fields, models
from odoo.exceptions import ValidationError


class DealSubmissionWindow(models.Model):
    _name = 'mint.deal.submission.window'
    _description = 'Deal Submission Plot Window'
    _order = 'sequence, date_start, id'

    submission_id = fields.Many2one(
        'mint.deal.submission',
        string='Submission',
        required=True,
        ondelete='cascade',
        index=True,
    )
    sequence = fields.Integer(string='Sequence', default=10)
    date_start = fields.Date(string='Start Date', required=True)
    date_end = fields.Date(string='End Date', required=True)
    day_count = fields.Integer(
        string='Days',
        compute='_compute_day_count',
    )

    @api.depends('date_start', 'date_end')
    def _compute_day_count(self):
        for rec in self:
            if rec.date_start and rec.date_end and rec.date_end >= rec.date_start:
                rec.day_count = (rec.date_end - rec.date_start).days + 1
            else:
                rec.day_count = 0

    @api.constrains('date_start', 'date_end')
    def _check_date_range(self):
        for rec in self:
            if rec.date_start and rec.date_end and rec.date_start > rec.date_end:
                raise ValidationError(
                    f"Window start ({rec.date_start}) must be on or before "
                    f"end ({rec.date_end})."
                )
