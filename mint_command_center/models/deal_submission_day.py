from odoo import fields, models


class MintDealSubmissionDay(models.Model):
    """A single calendar day the vendor wants their deal to run.

    Lives as an o2m child of mint.deal.submission so the vendor can select
    non-contiguous days (e.g. Mon + Wed + Fri across two weeks) — replacing
    the legacy free-text preferred_days char.
    """

    _name = 'mint.deal.submission.day'
    _description = 'Vendor Deal Submission — Requested Plot Day'
    _order = 'submission_id, date, id'

    submission_id = fields.Many2one(
        'mint.deal.submission',
        string='Submission',
        required=True,
        index=True,
        ondelete='cascade',
    )
    date = fields.Date(
        string='Date',
        required=True,
    )
