"""
Brand Reorder Builder wizard (F8).

Given a brand + store selection, produce one recommendation line per
(store, product) combo with:
  - qty_on_hand (from stock.quant, summed across the store's locations)
  - avg_daily_sales (from F7 product.template.x_avg_daily_sales)
  - days_of_supply (qty_on_hand / avg_daily_sales)
  - recommended_reorder_qty (target a configurable days-of-supply, default 14)

Sorted ascending by days_of_supply so the lowest-supply items reorder
first. Output exports: CSV download (text/csv response) and
copy-to-clipboard (TSV via JS).

This wizard reads from data captured by mint_pos_bridge (POS lines) and
the existing stock module (stock.quant). It does NOT call Dutchie at
runtime — no external API dep, no scope keys required.
"""

import csv
import io
import logging

from odoo import api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


# Default target days-of-supply when computing reorder qty. Configurable
# via ir.config_parameter `mint_command_center.reorder_target_days`.
DEFAULT_TARGET_DAYS = 14


class BrandReorderWizard(models.TransientModel):
    _name = 'mint.brand.reorder.wizard'
    _description = 'Brand Reorder Builder — per-store reorder recommendations'

    brand_id = fields.Many2one(
        'mint.brand',
        string='Brand',
        required=True,
    )
    store_ids = fields.Many2many(
        'res.company',
        string='Stores',
        domain="[('is_dispensary', '=', True)]",
        help='Leave empty to include every dispensary.',
    )
    target_days_of_supply = fields.Integer(
        string='Target Days of Supply',
        default=DEFAULT_TARGET_DAYS,
        help='Recommended reorder qty aims to bring on-hand inventory to '
             'this many days of supply at the current avg daily sales.',
    )
    line_ids = fields.One2many(
        'mint.brand.reorder.line',
        'wizard_id',
        string='Reorder Lines',
    )
    line_count = fields.Integer(
        compute='_compute_line_count',
        string='# Recommendations',
    )

    @api.depends('line_ids')
    def _compute_line_count(self):
        for wiz in self:
            wiz.line_count = len(wiz.line_ids)

    # ─── Action: build the recommendation list ───────────────────────────

    def action_build(self):
        """Compute reorder lines for the selected brand + stores."""
        self.ensure_one()
        if not self.brand_id:
            raise UserError("Brand is required.")

        # Clear any previous run.
        self.line_ids.unlink()

        # Resolve stores
        stores = self.store_ids
        if not stores:
            stores = self.env['res.company'].sudo().search([
                ('is_dispensary', '=', True),
            ])
        if not stores:
            raise UserError("No dispensary companies found to reorder for.")

        # Fetch products for this brand
        Product = self.env['product.template'].sudo()
        products = Product.search([('brand_id', '=', self.brand_id.id)])
        if not products:
            raise UserError(
                f"No products linked to brand \"{self.brand_id.name}\"."
            )

        # Pull stock-quant qty per (product, store) once via SQL — much
        # faster than iterating quants in Python.
        cr = self.env.cr
        cr.execute("""
            SELECT
                q.company_id,
                pp.product_tmpl_id,
                COALESCE(SUM(q.quantity - q.reserved_quantity), 0) AS qty
              FROM stock_quant q
              JOIN product_product pp ON pp.id = q.product_id
              JOIN stock_location loc ON loc.id = q.location_id
             WHERE loc.usage = 'internal'
               AND pp.product_tmpl_id = ANY(%s)
               AND q.company_id = ANY(%s)
             GROUP BY q.company_id, pp.product_tmpl_id
        """, (products.ids, stores.ids))
        qty_map = {(row[0], row[1]): float(row[2] or 0) for row in cr.fetchall()}

        Line = self.env['mint.brand.reorder.line'].sudo()
        target_days = max(1, int(self.target_days_of_supply or DEFAULT_TARGET_DAYS))

        rows_created = 0
        for store in stores:
            for product in products:
                qty = qty_map.get((store.id, product.id), 0.0)
                adv = float(product.x_avg_daily_sales or 0.0)
                if adv <= 0 and qty <= 0:
                    # Both zero — no signal at all for this (store, product).
                    continue
                days = (qty / adv) if adv > 0 else 999.0
                target_qty = target_days * adv
                recommend = max(0.0, target_qty - qty)
                Line.create({
                    'wizard_id': self.id,
                    'store_id': store.id,
                    'product_id': product.id,
                    'qty_on_hand': qty,
                    'avg_daily_sales': adv,
                    'days_of_supply': round(days, 2),
                    'recommended_reorder_qty': round(recommend, 2),
                })
                rows_created += 1

        if not rows_created:
            raise UserError(
                f"No reorder signal for brand \"{self.brand_id.name}\" "
                f"across the selected stores — every (store, product) "
                f"has zero on-hand AND zero 30-day sales. Either the "
                f"velocity cron hasn't run yet or this brand isn't "
                f"stocked anywhere."
            )

        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }

    # ─── Export: CSV download ────────────────────────────────────────────

    def action_export_csv(self):
        """Return an act_url that hits the CSV controller for this wizard."""
        self.ensure_one()
        if not self.line_ids:
            raise UserError("Build the recommendation list first.")
        return {
            'type': 'ir.actions.act_url',
            'url': f'/mint/brand-reorder/{self.id}/export.csv',
            'target': 'new',
        }


class BrandReorderLine(models.TransientModel):
    _name = 'mint.brand.reorder.line'
    _description = 'One reorder recommendation row'
    _order = 'days_of_supply, id'

    wizard_id = fields.Many2one(
        'mint.brand.reorder.wizard',
        ondelete='cascade',
        required=True,
    )
    store_id = fields.Many2one('res.company', string='Store', required=True)
    product_id = fields.Many2one('product.template', string='Product', required=True)
    qty_on_hand = fields.Float(string='On Hand', digits=(12, 2))
    avg_daily_sales = fields.Float(string='Avg/Day', digits=(12, 4))
    days_of_supply = fields.Float(string='Days Supply', digits=(8, 2))
    recommended_reorder_qty = fields.Float(
        string='Reorder Qty',
        digits=(12, 2),
        help='Bring inventory up to wizard.target_days_of_supply at the '
             'current avg daily sales rate. Rounded to 2 decimal places.',
    )


# ─── Controller for CSV download ──────────────────────────────────────────


from odoo import http  # noqa: E402
from odoo.http import request  # noqa: E402


class BrandReorderExportController(http.Controller):

    @http.route(
        '/mint/brand-reorder/<int:wiz_id>/export.csv',
        type='http', auth='user', methods=['GET'], csrf=False,
    )
    def export_csv(self, wiz_id, **kw):
        wiz = request.env['mint.brand.reorder.wizard'].browse(wiz_id).exists()
        if not wiz or not wiz.line_ids:
            return request.not_found()

        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator='\n')
        writer.writerow([
            'Store', 'Product', 'On Hand', 'Avg/Day',
            'Days Supply', 'Recommended Reorder Qty',
        ])
        for line in wiz.line_ids.sorted('days_of_supply'):
            writer.writerow([
                line.store_id.name,
                line.product_id.name,
                line.qty_on_hand,
                line.avg_daily_sales,
                line.days_of_supply,
                line.recommended_reorder_qty,
            ])

        body = buf.getvalue()
        brand_slug = (wiz.brand_id.name or 'brand').lower().replace(' ', '-')
        return request.make_response(
            body,
            headers=[
                ('Content-Type', 'text/csv; charset=utf-8'),
                ('Content-Disposition',
                 f'attachment; filename="reorder-{brand_slug}.csv"'),
                ('Cache-Control', 'no-store'),
            ],
        )
