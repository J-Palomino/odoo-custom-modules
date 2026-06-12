from odoo import api, fields, models


class MintTrendTerm(models.Model):
    """A tracked entity in the industry-trends watchlist — a brand, strain,
    product category, or topic we want to follow across sources.

    The collector matches scraped items against `keywords` to attach
    observations to the right term. `trend_score` is a stored rolling indicator
    (latest search-trend value, or recent mention volume) so list/kanban views
    can sort "what's hot right now" without re-aggregating on every read."""

    _name = 'mint.trend.term'
    _description = 'Industry Trend Term'
    _inherit = ['mail.thread']
    _order = 'trend_score desc, name'

    name = fields.Char(string='Term', required=True, tracking=True)
    term_type = fields.Selection(
        selection=[
            ('brand', 'Brand'),
            ('strain', 'Strain'),
            ('category', 'Category'),
            ('topic', 'Topic'),
            ('competitor', 'Competitor'),
        ],
        string='Type',
        required=True,
        index=True,
        tracking=True,
    )
    keywords = fields.Char(
        string='Keywords',
        help='Comma-separated aliases the collector matches against scraped '
             'items (case-insensitive). Defaults to the term name if blank.',
    )
    market_id = fields.Many2one(
        'mint.region',
        string='Market',
        index=True,
        help='Optional market scope. Leave blank to track nationally.',
    )
    source_ids = fields.Many2many(
        'mint.trend.source',
        string='Sources',
        help='Which Apify sources feed this term. Used by the collector to '
             'inject this term into the relevant actor runs.',
    )
    active = fields.Boolean(string='Active', default=True, tracking=True)

    observation_ids = fields.One2many(
        'mint.trend.observation',
        'term_id',
        string='Observations',
    )
    observation_count = fields.Integer(
        string='Observations',
        compute='_compute_stats',
    )
    last_observed_on = fields.Date(
        string='Last Observed',
        compute='_compute_stats',
        store=True,
    )
    trend_score = fields.Float(
        string='Trend Score',
        compute='_compute_stats',
        store=True,
        index=True,
        help='Latest search-trend index for this term, or recent mention '
             'volume when no search signal exists. Refreshed by the daily '
             'housekeeping cron.',
    )
    notes = fields.Text(string='Notes')

    @api.depends('observation_ids.value', 'observation_ids.observed_on',
                 'observation_ids.signal_type')
    def _compute_stats(self):
        for term in self:
            obs = term.observation_ids
            term.observation_count = len(obs)
            term.last_observed_on = max(obs.mapped('observed_on'), default=False)
            # Prefer the most recent search-trend index as the headline score;
            # fall back to count of observations in the last 30 days.
            search = obs.filtered(lambda o: o.signal_type == 'search_trend' and o.observed_on)
            if search:
                latest = max(search, key=lambda o: o.observed_on)
                term.trend_score = latest.value
            elif obs:
                cutoff = fields.Date.subtract(fields.Date.context_today(term), days=30)
                term.trend_score = float(len(obs.filtered(
                    lambda o: o.observed_on and o.observed_on >= cutoff
                )))
            else:
                term.trend_score = 0.0

    @api.model
    def _cron_refresh_scores(self):
        """Daily housekeeping: recompute rolling trend scores so kanban
        ordering stays fresh even on days a term gets no new observations."""
        terms = self.search([])
        terms._compute_stats()

    def action_view_observations(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': self.name,
            'res_model': 'mint.trend.observation',
            'view_mode': 'graph,pivot,list,form',
            'domain': [('term_id', '=', self.id)],
            'context': {'default_term_id': self.id},
        }
