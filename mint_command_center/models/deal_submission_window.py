from datetime import timedelta

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
            rec.day_count = (
                (rec.date_end - rec.date_start).days + 1
                if rec.date_start and rec.date_end
                else 0
            )

    @api.constrains('date_start', 'date_end')
    def _check_date_range(self):
        for rec in self:
            if rec.date_start and rec.date_end and rec.date_start > rec.date_end:
                raise ValidationError(
                    f"Window start ({rec.date_start}) must be on or before "
                    f"end ({rec.date_end})."
                )

    def _iter_dates(self):
        """Yield every date in this window (inclusive)."""
        self.ensure_one()
        if not (self.date_start and self.date_end):
            return
        cur = self.date_start
        while cur <= self.date_end:
            yield cur
            cur += timedelta(days=1)

    def all_dates(self):
        """Return the deduped union of all dates across this recordset,
        as ISO-format strings, in window-order (sequence, date_start)."""
        seen = []
        seen_set = set()
        for w in self:
            for d in w._iter_dates():
                iso = d.isoformat()
                if iso not in seen_set:
                    seen.append(iso)
                    seen_set.add(iso)
        return seen
