from odoo import api, fields, models


class MintCompetitorDeal(models.Model):
    """A deal observed at a competitor — captured by field reps or via
    monitoring. `is_active` reflects whether the deal hasn't expired yet so
    list views can default to "what's live at competitors right now"."""

    _name = 'mint.competitor.deal'
    _description = 'Competitor Deal'
    _order = 'captured_at desc, id desc'

    competitor_id = fields.Many2one(
        'mint.competitor',
        string='Competitor',
        required=True,
        index=True,
        ondelete='cascade',
    )
    deal_text = fields.Text(string='Deal', required=True)
    category = fields.Selection(
        selection=[
            ('flower', 'Flower'),
            ('pre_rolls', 'Pre-Rolls'),
            ('vapes', 'Vapes'),
            ('edibles', 'Edibles'),
            ('concentrates', 'Concentrates'),
            ('storewide', 'Store-wide'),
        ],
        string='Category',
        index=True,
    )
    source = fields.Selection(
        selection=[
            ('website', 'Website'),
            ('in_store', 'In-Store'),
            ('social', 'Social'),
            ('ad', 'Ad'),
        ],
        string='Source',
    )
    captured_at = fields.Datetime(
        string='Captured',
        required=True,
        default=fields.Datetime.now,
        index=True,
    )
    expires_at = fields.Date(string='Expires')
    is_active = fields.Boolean(
        string='Active',
        compute='_compute_is_active',
        store=True,
        index=True,
    )
    notes = fields.Text(string='Notes')

    market_id = fields.Many2one(
        related='competitor_id.market_id',
        store=True,
        index=True,
    )

    @api.depends('expires_at')
    def _compute_is_active(self):
        today = fields.Date.context_today(self)
        for rec in self:
            rec.is_active = not rec.expires_at or rec.expires_at >= today
