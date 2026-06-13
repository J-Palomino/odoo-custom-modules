"""Structured bundle tiers (#93677 Cluster C, Phase C1).

A bundle deal like "2 for $18 or 3 for $25" is a list of (qty, price) tiers
rendered literally on the public PTL — never per-unit math. The tiers live as
child rows so both surfaces (vendor submission and internal ptl.deal) stop
encoding them in free-text sales_details, where the #93677 research found the
data unrecoverably inconsistent.

Shape is shared via an abstract mixin; the concrete models only add their
parent FK, mirroring the mint.ptl.deal.option pattern from #93643.
"""
from odoo import api, fields, models

from .deal_mixins import format_bundle_tiers_text


class MintBundleTierMixin(models.AbstractModel):
    _name = 'mint.bundle.tier.mixin'
    _description = 'Mixin: one qty/price tier of a bundle deal'
    _order = 'sequence, id'

    sequence = fields.Integer(string='Sequence', default=10)
    qty = fields.Integer(string='Qty', required=True, default=2)
    price = fields.Float(string='Bundle Price ($)', required=True)
    display_text = fields.Char(
        string='Tier',
        compute='_compute_display_text',
        help='Literal customer-facing text for this tier, e.g. "2 for $18".',
    )

    @api.depends('qty', 'price')
    def _compute_display_text(self):
        for rec in self:
            rec.display_text = format_bundle_tiers_text([(rec.qty, rec.price)])


# Required=True on Integer/Float doesn't stop a 0 — enforce positivity at
# the DB so a half-filled tier row can't blank the literal render downstream.
_TIER_CHECKS = [
    ('qty_positive', 'CHECK(qty > 0)',
     'A bundle tier quantity must be at least 1.'),
    ('price_positive', 'CHECK(price > 0)',
     'A bundle tier price must be greater than $0.'),
]


class MintPtlDealBundleTier(models.Model):
    _name = 'mint.ptl.deal.bundle.tier'
    _description = 'PTL Deal Bundle Tier'
    _inherit = 'mint.bundle.tier.mixin'
    _sql_constraints = _TIER_CHECKS

    ptl_deal_id = fields.Many2one(
        'mint.ptl.deal',
        string='PTL Deal',
        required=True,
        ondelete='cascade',
        index=True,
    )


class MintDealSubmissionBundleTier(models.Model):
    _name = 'mint.deal.submission.bundle.tier'
    _description = 'Deal Submission Bundle Tier'
    _inherit = 'mint.bundle.tier.mixin'
    _sql_constraints = _TIER_CHECKS

    submission_id = fields.Many2one(
        'mint.deal.submission',
        string='Deal Submission',
        required=True,
        ondelete='cascade',
        index=True,
    )
