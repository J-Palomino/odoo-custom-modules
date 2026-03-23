from odoo import fields, models


class BrandRanking(models.Model):
    _name = 'mint.brand.ranking'
    _description = 'Brand Ranking — Brand performance per store/period'
    _order = 'ranking_date desc, rank'

    brand_id = fields.Many2one(
        'res.partner',
        string='Brand',
        required=True,
        index=True,
    )
    company_id = fields.Many2one(
        'res.company',
        string='Store',
        required=True,
        index=True,
    )
    rank = fields.Integer(string='Rank', required=True)
    ranking_date = fields.Date(
        string='Ranking Date',
        required=True,
        index=True,
    )
    period = fields.Selection(
        selection=[
            ('weekly', 'Weekly'),
            ('monthly', 'Monthly'),
            ('quarterly', 'Quarterly'),
        ],
        string='Period',
        default='weekly',
    )
    score = fields.Float(string='Score')
    notes = fields.Text(string='Notes')

    _brand_company_date_uniq = models.Constraint(
        'unique(brand_id, company_id, ranking_date, period)',
        'Only one ranking per brand per store per date/period.',
    )
