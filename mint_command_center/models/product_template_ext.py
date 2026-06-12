import logging
from datetime import timedelta

import psycopg2

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    # ─── F6a: Display label override (budtender-facing) ──────────────────

    x_display_label_override = fields.Char(
        string='PTL Display Label Override',
        size=128,
        help='Override the bud-tender-facing display label for this product on '
             'PTL calendars and exports. Leave empty to use the canonical product '
             'name. Used for surgical naming fixes without renaming the underlying '
             'product (e.g., distinguishing weight variants in a budtender menu).',
    )

    def _get_ptl_display_label(self):
        """Return the budtender-facing label: override if set, else product name."""
        self.ensure_one()
        return self.x_display_label_override or self.name or ''

    # ─── Format rollup: product line (master data) ──────────────────────

    x_product_line = fields.Char(
        string='Product Line',
        size=128,
        index=True,
        help='Product line within a brand (e.g. "Live Resin", "Solventless"). '
             'Mastered here on the product, not on the PTL deal, so the same '
             'product always rolls up under one format. Combined with brand + '
             'weight + MSRP to form the PTL format key (mint.ptl.deal.format_key) '
             'that collapses strain variants into a single public PTL row.',
    )

    # ─── F7: Sales velocity (computed nightly from mint.pos.order.line) ──

    x_units_sold_30d = fields.Integer(
        string='Units Sold (30d)',
        default=0,
        help='Total units sold across all Mint stores in the last 30 days, '
             'derived from mint.pos.order.line. Refreshed nightly by the '
             '"Velocity Compute" cron.',
    )
    x_revenue_30d = fields.Float(
        string='Revenue (30d)',
        default=0.0,
        digits=(12, 2),
        help='Sum of line_total across all 30-day POS lines for this product.',
    )
    x_avg_daily_sales = fields.Float(
        string='Avg Daily Sales (30d)',
        default=0.0,
        digits=(12, 4),
        help='units_30d / 30. Used by the Brand Reorder Builder to compute '
             'days-of-supply.',
    )
    x_velocity_rank = fields.Integer(
        string='Velocity Rank',
        index=True,
        default=0,
        help='Dense rank by units_sold_30d, descending. 1 = fastest mover. '
             'Recomputed by the same nightly cron.',
    )
    x_velocity_updated_at = fields.Datetime(
        string='Velocity Updated At',
        readonly=True,
        help='Last successful run of _cron_compute_velocity.',
    )

    @api.model
    def _cron_compute_velocity(self):
        """Aggregate mint.pos.order.line for the last 30 days into per-product
        velocity fields + dense rank.

        Batched + retry: a single bulk UPDATE over ~63k product_template rows
        runs under Odoo's REPEATABLE READ isolation and deterministically hits
        psycopg2 SerializationFailure against the constant product_template
        writers (Dutchie/inventory/order sync), rolling the whole job back. So
        we stage desired values in a session temp table (reads only — no
        write-lock, no conflict), then apply the writes in small id-range
        batches, each its own committed transaction with retry. See
        [cron-health:velocity] / Odoo task 94273.

        NOTE: the live prod cron runs an inline-SQL copy of this logic in its
        ir.cron code field (cache-trap workaround); this method is the
        source-of-truth mirror so a reinstall/repoint can't reintroduce the
        non-batched version.
        """
        _logger.info('Velocity Compute: starting (batched)')
        cutoff = fields.Datetime.now() - timedelta(days=30)
        cr = self.env.cr

        # 1. Stage desired final state for every product_template row. This
        #    reads product_template + mint_pos_order_line but only writes the
        #    session-local temp table, so it cannot serialization-conflict.
        cr.execute("DROP TABLE IF EXISTS _velo_stage")
        cr.execute("""
            CREATE TEMP TABLE _velo_stage AS
            WITH agg AS (
                SELECT pt.id AS tmpl_id,
                       SUM(line.quantity)::int AS units,
                       SUM(line.line_total)::numeric(12,2) AS revenue
                  FROM mint_pos_order_line line
                  JOIN product_template pt
                    ON pt.x_dutchie_product_id::text = line.dutchie_product_id
                 WHERE line.dutchie_product_id IS NOT NULL
                   AND line.dutchie_product_id <> ''
                   AND pt.x_dutchie_product_id IS NOT NULL
                   AND pt.x_dutchie_product_id <> 0
                   AND line.create_date >= %s
                 GROUP BY pt.id
            ),
            base AS (
                SELECT pt.id AS tmpl_id,
                       COALESCE(a.units, 0) AS units,
                       COALESCE(a.revenue, 0)::double precision AS revenue
                  FROM product_template pt
                  LEFT JOIN agg a ON a.tmpl_id = pt.id
            )
            SELECT tmpl_id, units, revenue,
                   (units / 30.0)::double precision AS avg_daily,
                   CASE WHEN units > 0
                        THEN DENSE_RANK() OVER (ORDER BY units DESC)
                        ELSE 0 END AS rank
              FROM base
        """, (cutoff,))
        cr.execute("CREATE INDEX _velo_stage_idx ON _velo_stage (tmpl_id)")
        cr.commit()

        # 2. Apply in small id-range batches, each its own committed txn with
        #    retry-on-serialization. Only touch rows whose values changed.
        cr.execute(
            "SELECT COALESCE(MIN(tmpl_id), 0), COALESCE(MAX(tmpl_id), 0) "
            "FROM _velo_stage")
        lo, hi = cr.fetchone()
        batch, retries = 4000, 8
        i = lo
        while i <= hi:
            for attempt in range(retries):
                try:
                    cr.execute("""
                        UPDATE product_template pt
                           SET x_units_sold_30d  = s.units,
                               x_revenue_30d     = s.revenue,
                               x_avg_daily_sales = s.avg_daily,
                               x_velocity_rank   = s.rank,
                               x_velocity_updated_at = CASE WHEN s.units > 0
                                   THEN NOW() ELSE pt.x_velocity_updated_at END
                          FROM _velo_stage s
                         WHERE pt.id = s.tmpl_id
                           AND pt.id >= %s AND pt.id < %s
                           AND (pt.x_units_sold_30d IS DISTINCT FROM s.units
                             OR pt.x_revenue_30d    IS DISTINCT FROM s.revenue
                             OR pt.x_velocity_rank  IS DISTINCT FROM s.rank)
                    """, (i, i + batch))
                    cr.commit()
                    break
                except (psycopg2.errors.SerializationFailure,
                        psycopg2.errors.DeadlockDetected):
                    cr.rollback()
                    if attempt == retries - 1:
                        raise
            i += batch

        # 3. Guaranteed daily freshness heartbeat: always stamp the current
        #    top mover (1 row) so x_velocity_updated_at advances even on a day
        #    where no ranks moved.
        for attempt in range(retries):
            try:
                cr.execute("""
                    UPDATE product_template SET x_velocity_updated_at = NOW()
                     WHERE id = (SELECT tmpl_id FROM _velo_stage
                                  WHERE rank = 1 LIMIT 1)
                """)
                cr.commit()
                break
            except psycopg2.errors.SerializationFailure:
                cr.rollback()
                if attempt == retries - 1:
                    raise

        cr.execute("DROP TABLE IF EXISTS _velo_stage")
        cr.commit()
        _logger.info('Velocity Compute: done (batched), cutoff=%s', cutoff)
        return True
