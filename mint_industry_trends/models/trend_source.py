from odoo import api, fields, models


class MintTrendSource(models.Model):
    """A configured Apify scrape that feeds industry-trend observations.

    Each source maps 1:1 to an Apify actor run. The actual scraping happens in
    the external k8s collector (`k8s/monitoring/industry-trends`), which reads
    the active sources here, runs the actor via the Apify API, and writes the
    results back as `mint.trend.observation` rows. `last_run_at`/`last_status`
    let non-devs see in the backend whether each feed is healthy without
    digging through cron logs."""

    _name = 'mint.trend.source'
    _description = 'Industry Trend Source (Apify)'
    _inherit = ['mail.thread']
    _order = 'signal_type, name'

    name = fields.Char(string='Name', required=True, tracking=True)
    signal_type = fields.Selection(
        selection=[
            ('search_trend', 'Search Trend'),
            ('social_mention', 'Social / Reddit'),
            ('news', 'News'),
            ('product_launch', 'Brand / Product Launch'),
            ('competitor_price', 'Competitor Menu / Pricing'),
        ],
        string='Signal Type',
        required=True,
        index=True,
        tracking=True,
        help='What kind of industry signal this source produces.',
    )
    apify_actor = fields.Char(
        string='Apify Actor',
        required=True,
        help='Apify actor id or "username/actor-name" slug the collector runs '
             '(e.g. "emastra/google-trends-scraper").',
    )
    apify_input = fields.Text(
        string='Apify Input (JSON)',
        help='JSON passed verbatim as the actor run input. Search terms are '
             'usually injected from the linked trend terms at run time.',
    )
    market_id = fields.Many2one(
        'mint.region',
        string='Market',
        index=True,
        help='Optional market scope. Leave blank for national signals.',
    )
    active = fields.Boolean(string='Active', default=True, tracking=True)

    last_run_at = fields.Datetime(string='Last Run', readonly=True)
    last_status = fields.Selection(
        selection=[
            ('ok', 'OK'),
            ('partial', 'Partial'),
            ('error', 'Error'),
        ],
        string='Last Status',
        readonly=True,
    )
    last_item_count = fields.Integer(string='Last Item Count', readonly=True)
    last_error = fields.Text(string='Last Error', readonly=True)

    observation_ids = fields.One2many(
        'mint.trend.observation',
        'source_id',
        string='Observations',
    )
    observation_count = fields.Integer(
        string='Observations',
        compute='_compute_observation_count',
    )
    notes = fields.Text(string='Notes')

    def _compute_observation_count(self):
        grouped = self.env['mint.trend.observation']._read_group(
            [('source_id', 'in', self.ids)],
            groupby=['source_id'],
            aggregates=['__count'],
        )
        counts = {source.id: count for source, count in grouped}
        for rec in self:
            rec.observation_count = counts.get(rec.id, 0)

    def action_view_observations(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': self.name,
            'res_model': 'mint.trend.observation',
            'view_mode': 'list,form,graph,pivot',
            'domain': [('source_id', '=', self.id)],
            'context': {'default_source_id': self.id},
        }
