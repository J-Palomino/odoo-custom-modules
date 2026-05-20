import logging
from datetime import timedelta

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
        """Aggregate mint.pos.order.line for last 30 days, write per-product
        velocity fields, recompute rank.

        Performance note: one direct SQL aggregation + one bulk update so we
        don't spin per-record ORM writes across thousands of SKUs.
        """
        _logger.info('Velocity Compute: starting')
        cutoff = fields.Datetime.now() - timedelta(days=30)
        cr = self.env.cr

        # Aggregate units + revenue per product.template via:
        #   mint.pos.order.line.dutchie_product_id → product.template.x_dutchie_product_id
        # Skip lines without a dutchie id; they can't link back.
        cr.execute("""
            WITH aggregated AS (
                SELECT
                    pt.id AS tmpl_id,
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
            )
            UPDATE product_template pt
               SET x_units_sold_30d = COALESCE(a.units, 0),
                   x_revenue_30d    = COALESCE(a.revenue, 0),
                   x_avg_daily_sales = COALESCE(a.units, 0) / 30.0
              FROM aggregated a
             WHERE pt.id = a.tmpl_id
        """, (cutoff,))
        updated = cr.rowcount

        # Zero out anything that didn't show up in the 30-day window so stale
        # values from previous runs don't linger.
        cr.execute("""
            UPDATE product_template pt
               SET x_units_sold_30d = 0,
                   x_revenue_30d = 0,
                   x_avg_daily_sales = 0
             WHERE NOT EXISTS (
                 SELECT 1 FROM mint_pos_order_line line
                  WHERE pt.x_dutchie_product_id::text = line.dutchie_product_id
                    AND line.dutchie_product_id IS NOT NULL
                    AND line.create_date >= %s
             ) AND pt.x_units_sold_30d > 0
        """, (cutoff,))
        zeroed = cr.rowcount

        # Recompute dense rank by units desc (1 = fastest mover).
        # Products with units=0 get rank=0 so they sort to the bottom and
        # the index can use rank>0 as a "has any velocity" filter.
        cr.execute("""
            WITH ranked AS (
                SELECT id, DENSE_RANK() OVER (ORDER BY x_units_sold_30d DESC) AS rk
                  FROM product_template
                 WHERE x_units_sold_30d > 0
            )
            UPDATE product_template pt
               SET x_velocity_rank = r.rk
              FROM ranked r
             WHERE pt.id = r.id
        """)
        cr.execute("""
            UPDATE product_template
               SET x_velocity_rank = 0
             WHERE x_units_sold_30d = 0 AND x_velocity_rank > 0
        """)

        # Stamp the timestamp on touched products only (avoid bumping 80k
        # rows just to record "last computed"; the per-row stamp is on the
        # ones that actually have current sales).
        now = fields.Datetime.now()
        cr.execute("""
            UPDATE product_template
               SET x_velocity_updated_at = %s
             WHERE x_units_sold_30d > 0
        """, (now,))

        _logger.info(
            'Velocity Compute: %d products updated, %d zeroed, cutoff=%s',
            updated, zeroed, cutoff,
        )
        return {'updated': updated, 'zeroed': zeroed, 'cutoff': str(cutoff)}
