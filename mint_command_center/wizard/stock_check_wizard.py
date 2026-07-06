import json
import logging
import urllib.request

from odoo import api, fields, models
from markupsafe import Markup

from ..models.brand_calendar import _brand_lookup_key
from ..models.ptl_deal import MASTER_CATEGORY_PATTERNS

_logger = logging.getLogger(__name__)

# A store counts as carrying a deal when the summed quantity_available across
# matching packages of a product is greater than this floor. Dutchie reports
# quantity per package (often small, e.g. "2"), so a high per-package floor
# silently drops in-stock products — aggregate per product and require > 0.
MIN_UNITS_IN_STOCK = 0

# Location-coverage bands for the per-deal stock_status. A deal is only
# "out_of_stock" when carried at zero locations; "in_stock" once it is carried
# at >= IN_STOCK_PCT of the market's stores; "low_stock" in between.
IN_STOCK_PCT = 50


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
        # Default to the LetsGoMint instance (-6aa5) — the cache the storefront
        # reads and every other mint_command_center flow writes to. The
        # no-suffix host is a separate instance with its own Redis.
        base_url = get_param('mint.inventory_service.url',
                             'https://mintinvsvc-production-6aa5.up.railway.app')
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
                url = f"{base_url}/api/locations/{uuid}/inventory?limit=5000"
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
            if locations_in == 0:
                status = 'out_of_stock'
            elif pct >= IN_STOCK_PCT:
                status = 'in_stock'
            else:
                status = 'low_stock'

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
        """True when this deal's brand + master-category bucket is carried
        (summed quantity_available > MIN_UNITS_IN_STOCK) at one location.

        Mirrors mint.ptl.deal's master-category resolution: the deal's
        product_category is a master bucket (e.g. "Vapes", "Pre-Rolls",
        "Edibles & Tinctures"), so we test the bucket's MASTER_CATEGORY_PATTERNS
        fragments against the Dutchie item's combined category text
        (master_category + category + category_path) rather than comparing the
        bucket label to a single sub-category string — the bucket labels only
        ever substring-match "Flower" otherwise. Brand is compared via the same
        normalized key used to dedupe brands, so punctuation/spacing variants
        ("TRU-Infusion" vs "Tru Infusion") match. Quantities are summed per
        product so small per-package counts still total in-stock. "Featured
        Deals" (empty pattern list) is brand-only, no category gate.
        """
        if not inventory_items:
            return False

        # Multi-brand (#93635): match inventory against ANY of the deal's
        # brands, not just the primary.
        deal_brands = (deal.brand_ids | deal.brand_id) if deal.brand_id else deal.brand_ids
        deal_brand_keys = {_brand_lookup_key(b.name) for b in deal_brands if b.name}
        deal_brand_keys.discard('')
        # None  -> legacy/non-master free-text category (exact-ish fallback)
        # []    -> "Featured Deals": brand-only, no category gate
        # [...] -> standard master bucket: match any fragment
        patterns = (MASTER_CATEGORY_PATTERNS.get(deal.product_category)
                    if deal.product_category else None)

        # Refuse to match every product when the deal has neither a brand nor a
        # category to constrain on — that would mark unconstrained deals as
        # in_stock against any inventory at all.
        if not deal_brand_keys and not deal.product_category:
            return False

        qty_by_product = {}
        for item in inventory_items:
            if deal_brand_keys:
                item_brand_key = _brand_lookup_key(
                    item.get('brand_name') or item.get('brandName') or '')
                if not item_brand_key:
                    continue
                if not any(k in item_brand_key or item_brand_key in k
                           for k in deal_brand_keys):
                    continue

            if patterns:
                combined = ' '.join(
                    str(item.get(f) or '')
                    for f in ('master_category', 'category', 'category_path')
                ).lower()
                if not any(p.lower() in combined for p in patterns):
                    continue
            elif patterns is None and deal.product_category:
                combined = ' '.join(
                    str(item.get(f) or '')
                    for f in ('master_category', 'category', 'category_path')
                ).lower()
                cat = deal.product_category.lower()
                if cat not in combined and combined not in cat:
                    continue

            raw_qty = (
                item.get('quantity_available')
                or item.get('quantityAvailable')
                or item.get('quantity_on_hand')
                or 0
            )
            try:
                qty = float(raw_qty)
            except (TypeError, ValueError):
                qty = 0
            # Fall back to the item's own identity when no product id is present
            # so quantities still aggregate per distinct package.
            pid = item.get('product_id') or item.get('id') or id(item)
            qty_by_product[pid] = qty_by_product.get(pid, 0) + qty

        return any(q > MIN_UNITS_IN_STOCK for q in qty_by_product.values())

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
