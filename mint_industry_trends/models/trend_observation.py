from odoo import api, fields, models


class MintTrendObservation(models.Model):
    """A single industry-trend datapoint written by the Apify collector.

    Rows are deduped on (`source_id`, `external_id`) so re-running an actor is
    idempotent. `value`/`value_unit` hold the metric (search index 0-100,
    mention count, sentiment, price in USD); event-style signals (news, social,
    launches) carry their headline in `title` and a link in `url`."""

    _name = 'mint.trend.observation'
    _description = 'Industry Trend Observation'
    _order = 'observed_on desc, id desc'

    term_id = fields.Many2one(
        'mint.trend.term',
        string='Term',
        index=True,
        ondelete='cascade',
    )
    term_text = fields.Char(
        string='Matched Term',
        help='Raw term/keyword the collector matched, even when no '
             'watchlist term was linked.',
    )
    source_id = fields.Many2one(
        'mint.trend.source',
        string='Source',
        required=True,
        index=True,
        ondelete='cascade',
    )
    signal_type = fields.Selection(
        related='source_id.signal_type',
        store=True,
        index=True,
    )
    market_id = fields.Many2one(
        'mint.region',
        string='Market',
        index=True,
    )
    observed_on = fields.Date(
        string='Observed On',
        required=True,
        index=True,
        help='The day the metric applies to (not necessarily the capture day).',
    )
    captured_at = fields.Datetime(
        string='Captured',
        default=fields.Datetime.now,
        index=True,
    )
    value = fields.Float(string='Value')
    value_unit = fields.Char(
        string='Unit',
        help='index | mentions | USD | sentiment …',
    )
    sentiment = fields.Float(
        string='Sentiment',
        help='Optional -1.0 (negative) … 1.0 (positive).',
    )
    title = fields.Char(string='Title / Headline')
    url = fields.Char(string='URL')
    external_id = fields.Char(
        string='External ID',
        index=True,
        help='Apify dataset item id or a stable dedupe key from the source.',
    )
    raw_json = fields.Text(string='Raw Payload')

    _sql_constraints = [
        (
            'source_external_uniq',
            'unique(source_id, external_id)',
            'An observation with this external id already exists for the source.',
        ),
    ]

    @api.model
    def upsert(self, vals):
        """Idempotent create used by the collector: update an existing row when
        (source_id, external_id) already exists, else create. Returns the id."""
        source_id = vals.get('source_id')
        external_id = vals.get('external_id')
        if source_id and external_id:
            existing = self.search([
                ('source_id', '=', source_id),
                ('external_id', '=', external_id),
            ], limit=1)
            if existing:
                existing.write(vals)
                return existing.id
        return self.create(vals).id
