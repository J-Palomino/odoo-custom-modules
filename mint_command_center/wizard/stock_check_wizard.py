import json
import logging
import urllib.request

from odoo import api, fields, models
from markupsafe import Markup

_logger = logging.getLogger(__name__)

STOCK_THRESHOLD = 10  # Minimum units to count as "in stock"


class StockCheckWizard(models.TransientModel):
    _name = 'mint.stock.check.wizard'
    _description = 'PTL Stock Check'

    ptl_day_id = fields.Many2one('mint.ptl.day', string='PTL Day')
    deal_ids = fields.Many2many('mint.ptl.deal', string='Deals to Check')
    market_id = fields.Many2one('mint.region', string='Market')
    result_html = fields.Html(string='Results', readonly=True, sanitize=False)
    state = fields.Selection([
        ('setup', 'Setup'),
        ('done', 'Done'),
    ], default='setup')

    @api.onchange('ptl_day_id')
    def _onchange_ptl_day_id(self):
        if self.ptl_day_id:
            self.deal_ids = self.ptl_day_id.deal_ids
            self.market_id = self.ptl_day_id.market_id

    def action_check_stock(self):
        """Query inventory service API for stock availability per deal."""
        self.ensure_one()

        get_param = self.env['ir.config_parameter'].sudo().get_param
        base_url = get_param('mint.inventory_service.url',
                             'https://mintinvsvc-production.up.railway.app')
        api_key = get_param('mint.inventory_service.api_key', '')

        # Get retail stores for the market
        domain = [
            ('is_dispensary', '=', True),
            ('dutchie_store_id', '!=', False),
        ]
        if self.market_id:
            domain.append(('region_id', '=', self.market_id.id))
        stores = self.env['res.company'].sudo().search(domain)
        total_locations = len(stores)

        if not total_locations:
            self.write({
                'result_html': '<p class="text-warning">No retail stores found for this market.</p>',
                'state': 'done',
            })
            return self._reopen()

        # Fetch inventory per store
        store_inventories = {}  # uuid → [inventory items]
        for store in stores:
            uuid = store.dutchie_store_id
            try:
                url = f"{base_url}/api/locations/{uuid}/inventory"
                req = urllib.request.Request(url, headers={
                    'X-API-Key': api_key,
                    'Accept': 'application/json',
                })
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = json.loads(resp.read().decode('utf-8'))
                    items = data if isinstance(data, list) else data.get('items', data.get('data', []))
                    store_inventories[uuid] = items
            except Exception as e:
                _logger.warning('Stock check failed for %s (%s): %s', store.name, uuid[:12], e)
                store_inventories[uuid] = []

        # Check each deal against inventory
        results = []
        for deal in self.deal_ids:
            locations_in = 0
            for store in stores:
                uuid = store.dutchie_store_id
                items = store_inventories.get(uuid, [])
                if self._deal_in_stock(deal, items):
                    locations_in += 1

            pct = (locations_in / total_locations * 100) if total_locations else 0
            if pct >= 75:
                status = 'in_stock'
            elif pct >= 50:
                status = 'low_stock'
            else:
                status = 'out_of_stock'

            # Update deal record
            deal.write({
                'stock_status': status,
                'stock_locations_in': locations_in,
                'stock_locations_total': total_locations,
                'stock_checked_at': fields.Datetime.now(),
            })

            results.append({
                'deal': deal,
                'locations_in': locations_in,
                'total': total_locations,
                'pct': pct,
                'status': status,
            })

        self.write({
            'result_html': self._build_results_html(results),
            'state': 'done',
        })
        return self._reopen()

    def _deal_in_stock(self, deal, inventory_items):
        """Check if a deal's product/brand/category is in stock at a location."""
        if not inventory_items:
            return False

        for item in inventory_items:
            qty = item.get('quantityAvailable', item.get('quantity_on_hand', 0))
            if qty < STOCK_THRESHOLD:
                continue

            # Match by brand name (case-insensitive)
            if deal.brand_id:
                item_brand = (item.get('brandName') or item.get('brand_name') or '').lower()
                if deal.brand_id.name.lower() in item_brand or item_brand in deal.brand_id.name.lower():
                    # Also check category if specified
                    if deal.product_category:
                        item_cat = (item.get('category') or item.get('masterCategory') or '').lower()
                        if deal.product_category.lower() in item_cat or item_cat in deal.product_category.lower():
                            return True
                    else:
                        return True

            # Match by category only
            if deal.product_category and not deal.brand_id:
                item_cat = (item.get('category') or item.get('masterCategory') or '').lower()
                if deal.product_category.lower() in item_cat or item_cat in deal.product_category.lower():
                    return True

        return False

    def _build_results_html(self, results):
        """Build HTML table of stock check results."""
        status_display = {
            'in_stock': ('<span style="color:green">&#x2705; In Stock</span>', 'background-color:#d4edda'),
            'low_stock': ('<span style="color:orange">&#x26A0;&#xFE0F; Low Stock</span>', 'background-color:#fff3cd'),
            'out_of_stock': ('<span style="color:red">&#x274C; Out of Stock</span>', 'background-color:#f8d7da'),
        }

        rows = []
        for r in results:
            badge, bg = status_display.get(r['status'], ('', ''))
            rows.append(
                f'<tr style="{bg}">'
                f'<td>{r["deal"].name}</td>'
                f'<td>{r["deal"].brand_id.name if r["deal"].brand_id else "-"}</td>'
                f'<td>{r["deal"].product_category or "-"}</td>'
                f'<td>{r["locations_in"]} / {r["total"]}</td>'
                f'<td>{r["pct"]:.0f}%</td>'
                f'<td>{badge}</td>'
                f'</tr>'
            )

        html = (
            '<table class="table table-sm table-bordered">'
            '<thead><tr>'
            '<th>Deal</th><th>Brand</th><th>Category</th>'
            '<th>Locations</th><th>%</th><th>Status</th>'
            '</tr></thead>'
            '<tbody>' + ''.join(rows) + '</tbody>'
            '</table>'
        )
        return Markup(html)

    def _reopen(self):
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }
