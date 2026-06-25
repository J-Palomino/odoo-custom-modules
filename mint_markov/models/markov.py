import json
import logging
from datetime import datetime, timedelta

import requests

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

# Dutchie POS order_source values that originate online (i.e. can follow a
# storefront -> Dutchie handoff). "Walk In" is in-store and intentionally
# excluded. See mint.pos.order.order_source distribution.
DEFAULT_ONLINE_SOURCES = ('Dutchie', 'Leafly', 'Weedmaps')

# Terminal order states. Mirrors mint_pos_bridge._TERMINAL_STATES; a fulfilled
# order can land in any SUCCESS_STATES value (not just 'completed'), so all of
# them must count as a completion or P(completed) is understated and historical
# terminal orders leave a permanent fake "in_progress" residual.
SUCCESS_STATES = ('completed', 'picked_up', 'delivery_completed')
CANCELLED_STATES = ('cancelled',)

# Reused intent transition-matrix query (mirrors PostHog insight uoond6vO).
INTENT_HOGQL = """
WITH shopping AS (
  SELECT $session_id AS sid, timestamp AS ts, event AS state
  FROM events
  WHERE timestamp > now() - INTERVAL %(days)d DAY
    AND $session_id IS NOT NULL
    AND event IN ('page_viewed','store_selected','location_picker_opened',
                  'products_viewed','product_clicked','search_performed',
                  'add_to_cart','cart_viewed','checkout_page_viewed',
                  'external_link_clicked','id_scanned')
),
trans AS (
  SELECT state AS from_state,
         leadInFrame(state) OVER (
           PARTITION BY sid ORDER BY ts ASC
           ROWS BETWEEN CURRENT ROW AND 1 FOLLOWING
         ) AS to_state
  FROM shopping
),
counts AS (
  SELECT from_state, to_state, count() AS n
  FROM trans
  WHERE to_state != '' AND to_state != from_state
  GROUP BY from_state, to_state
)
SELECT from_state, to_state, n,
       round(n / sum(n) OVER (PARTITION BY from_state), 4) AS probability
FROM counts
ORDER BY from_state ASC, probability DESC
LIMIT 500
"""

HANDOFF_HOGQL = (
    "SELECT count() AS c FROM events "
    "WHERE timestamp > now() - INTERVAL %(days)d DAY "
    "AND event IN ('external_link_clicked','checkout_page_viewed')"
)


class MintMarkovSnapshot(models.Model):
    _name = 'mint.markov.snapshot'
    _description = 'Markov transition-matrix snapshot'
    _order = 'collected_at desc'

    name = fields.Char(required=True, default='Markov snapshot')
    collected_at = fields.Datetime(
        default=fields.Datetime.now, index=True, readonly=True)
    window_days = fields.Integer(
        default=30, help='Look-back window used for this snapshot.')
    intent_source_ok = fields.Boolean(
        string='PostHog intent fetched', readonly=True,
        help='True when the PostHog query API returned the intent matrix.')
    n_intent = fields.Integer(string='# intent transitions', readonly=True)
    n_buying = fields.Integer(string='# buying transitions', readonly=True)
    n_bridge = fields.Integer(string='# bridge transitions', readonly=True)
    note = fields.Text(readonly=True)
    transition_ids = fields.One2many(
        'mint.markov.transition', 'snapshot_id', string='Transitions')

    # ------------------------------------------------------------------
    # config helpers
    # ------------------------------------------------------------------
    def _cfg(self, key, default=None):
        return self.env['ir.config_parameter'].sudo().get_param(key, default)

    def _online_sources(self):
        raw = self._cfg('mint_markov.online_sources')
        if raw:
            vals = tuple(s.strip() for s in raw.split(',') if s.strip())
            if vals:
                return vals
        return DEFAULT_ONLINE_SOURCES

    # ------------------------------------------------------------------
    # cron entry point
    # ------------------------------------------------------------------
    @api.model
    def _cron_collect(self):
        window = int(self._cfg('mint_markov.window_days', 30) or 30)
        snap = self.create({
            'name': 'Markov %s' % fields.Date.context_today(self),
            'window_days': window,
        })
        notes = []
        Trans = self.env['mint.markov.transition']
        rows = []

        # 1. buying phase — always runs against local POS data
        try:
            buying, online_total = self._collect_buying(window)
            rows += [dict(r, snapshot_id=snap.id, phase='buying') for r in buying]
            snap.n_buying = len(buying)
            notes.append('buying: %s transitions over %s online orders'
                         % (len(buying), online_total))
        except Exception as exc:  # noqa: BLE001 - keep cron resilient
            online_total = 0
            _logger.exception('mint_markov: buying phase failed')
            notes.append('buying: FAILED (%s)' % exc)

        # 2. intent phase — only when a PostHog query key is configured
        handoffs = None
        try:
            intent, handoffs = self._collect_intent(window)
            if intent is not None:
                rows += [dict(r, snapshot_id=snap.id, phase='intent')
                         for r in intent]
                snap.n_intent = len(intent)
                snap.intent_source_ok = True
                notes.append('intent: %s transitions, %s handoffs'
                             % (len(intent), handoffs))
            else:
                notes.append('intent: skipped (no PostHog API key configured)')
        except Exception as exc:  # noqa: BLE001
            _logger.exception('mint_markov: intent phase failed')
            notes.append('intent: FAILED (%s)' % exc)

        # 3. bridge phase — needs both handoffs (PostHog) and orders (Odoo)
        if handoffs and online_total:
            prob = round(min(1.0, online_total / float(handoffs)), 4)
            rows.append({
                'snapshot_id': snap.id, 'phase': 'bridge',
                'from_state': 'external_link_clicked',
                'to_state': 'order_placed',
                'observations': online_total, 'probability': prob,
            })
            snap.n_bridge = 1
            notes.append('bridge: P(order|handoff)=%s' % prob)
        else:
            notes.append('bridge: skipped (need both handoffs and orders)')

        if rows:
            Trans.create(rows)
        snap.note = '\n'.join(notes)
        _logger.info('mint_markov snapshot %s: %s', snap.id, snap.note)
        return snap.id

    # ------------------------------------------------------------------
    # buying phase (local SQL over mint.pos.order)
    # ------------------------------------------------------------------
    def _collect_buying(self, window):
        since = datetime.utcnow() - timedelta(days=window)
        sources = self._online_sources()
        cr = self.env.cr

        cr.execute(
            """
            SELECT COALESCE(state, 'unknown') AS state, count(*) AS n
            FROM mint_pos_order
            WHERE create_date >= %(since)s AND order_source IN %(sources)s
            GROUP BY COALESCE(state, 'unknown')
            """,
            {'since': since, 'sources': tuple(sources)},
        )
        by_state = {row[0]: row[1] for row in cr.fetchall()}
        total = sum(by_state.values())
        if not total:
            return [], 0

        completed = sum(by_state.get(s, 0) for s in SUCCESS_STATES)
        cancelled = sum(by_state.get(s, 0) for s in CANCELLED_STATES)
        other = total - completed - cancelled

        out = []

        def add(from_s, to_s, n, denom):
            if n and denom:
                out.append({
                    'from_state': from_s, 'to_state': to_s,
                    'observations': n, 'probability': round(n / float(denom), 4),
                })

        add('order_placed', 'completed', completed, total)
        add('order_placed', 'cancelled', cancelled, total)
        add('order_placed', 'in_progress', other, total)

        # post-completion returns (best effort; guarded for cost/availability)
        try:
            cr.execute(
                """
                SELECT count(DISTINCT l.order_id)
                FROM mint_pos_order_line l
                JOIN mint_pos_order o ON o.id = l.order_id
                WHERE o.create_date >= %(since)s
                  AND o.order_source IN %(sources)s
                  AND o.state IN %(success)s
                  AND l.return_date IS NOT NULL
                """,
                {'since': since, 'sources': tuple(sources),
                 'success': SUCCESS_STATES},
            )
            returned = cr.fetchone()[0] or 0
            # numerator and denominator now share the success population,
            # so this is a true P(returned | completed) bounded at 1.0.
            add('completed', 'returned', returned, completed)
        except Exception:  # noqa: BLE001
            _logger.warning('mint_markov: returns sub-query skipped', exc_info=True)

        return out, total

    # ------------------------------------------------------------------
    # intent phase (PostHog query API)
    # ------------------------------------------------------------------
    def _posthog_query(self, hogql, params):
        base = self._cfg('mint_markov.posthog_base_url', 'https://us.posthog.com')
        pid = self._cfg('mint_markov.posthog_project_id', '290683')
        key = self._cfg('mint_markov.posthog_api_key')
        if not key:
            return None
        url = '%s/api/projects/%s/query/' % (base.rstrip('/'), pid)
        resp = requests.post(
            url,
            headers={'Authorization': 'Bearer %s' % key,
                     'Content-Type': 'application/json'},
            data=json.dumps({'query': {
                'kind': 'HogQLQuery', 'query': hogql % params}}),
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()

    def _collect_intent(self, window):
        data = self._posthog_query(INTENT_HOGQL, {'days': window})
        if data is None:
            return None, None
        cols = data.get('columns') or ['from_state', 'to_state', 'n', 'probability']
        idx = {c: i for i, c in enumerate(cols)}
        intent = []
        for row in data.get('results', []):
            intent.append({
                'from_state': row[idx['from_state']],
                'to_state': row[idx['to_state']],
                'observations': int(row[idx['n']]),
                'probability': float(row[idx['probability']]),
            })

        handoffs = None
        try:
            hd = self._posthog_query(HANDOFF_HOGQL, {'days': window})
            if hd and hd.get('results'):
                handoffs = int(hd['results'][0][0])
        except Exception:  # noqa: BLE001
            _logger.warning('mint_markov: handoff count failed', exc_info=True)
        return intent, handoffs


class MintMarkovTransition(models.Model):
    _name = 'mint.markov.transition'
    _description = 'Markov transition (one from->to edge in a snapshot)'
    _order = 'snapshot_id desc, phase, from_state, probability desc'

    snapshot_id = fields.Many2one(
        'mint.markov.snapshot', required=True, ondelete='cascade', index=True)
    collected_at = fields.Datetime(
        related='snapshot_id.collected_at', store=True, index=True)
    phase = fields.Selection(
        [('intent', 'Intent (PostHog)'),
         ('buying', 'Buying (POS)'),
         ('bridge', 'Bridge (handoff->order)')],
        required=True, index=True)
    from_state = fields.Char(required=True, index=True)
    to_state = fields.Char(required=True, index=True)
    observations = fields.Integer(help='Transition count behind the probability.')
    probability = fields.Float(
        digits=(12, 4), aggregator='avg',
        help='P(to_state | from_state) within this phase. Averaged (not summed) '
             'in pivots so cells stay in [0,1] when multiple snapshots overlap.')
