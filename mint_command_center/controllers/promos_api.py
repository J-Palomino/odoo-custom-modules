"""Public JSON helper endpoints for the /promos vendor deal form.

Two routes:

* GET /promos/products  → product picker options filtered by brand ∩ category
* POST /promos/conflicts → live conflict tooltip data, reusing the
                           (date × market) × (brand_id, product_category) overlap
                           shape from mint.ptl.conflict.resolution.wizard.

Pattern follows controllers/banner_api.py:17-23 — type='http', auth='public',
csrf=False, JSON via request.make_response(json.dumps(...)).
"""
import json
import logging
from datetime import datetime

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class PromosApiController(http.Controller):

    @http.route(
        '/promos/products',
        type='http',
        auth='public',
        methods=['GET'],
        csrf=False,
    )
    def list_products(self, brand_id=None, category=None, **_):
        """Return up to 200 product.template rows matching (brand_id, category).

        Both filters required — refuses to dump the whole catalog. Output is
        JSON list of {id, name, weight_value, weight_unit} so the form can
        auto-fill weight when a single product is selected.
        """
        try:
            brand_id_int = int(brand_id) if brand_id else None
        except (ValueError, TypeError):
            brand_id_int = None
        category = (category or '').strip()

        if not brand_id_int or not category:
            return request.make_response(
                json.dumps({'error': 'brand_id and category are required'}),
                headers=[('Content-Type', 'application/json')],
                status=400,
            )

        Product = request.env['product.template'].sudo()
        domain = [('brand_id', '=', brand_id_int)]
        # product_category mirrors the controller's PRODUCT_CATEGORIES values
        # (lowercase: flower, pre-rolls, …); match against product attributes
        # or category name with a case-insensitive contains.
        domain.append(('categ_id.name', 'ilike', category))

        products = Product.search_read(
            domain,
            ['id', 'name', 'weight'],
            limit=200,
            order='name',
        )
        # weight on product.template is the physical shipping weight — not the
        # cannabis weight we want. Leave weight_value/weight_unit blank; the
        # form can prompt the vendor to set it manually.
        out = [
            {
                'id': p['id'],
                'name': p['name'],
                'weight_value': 0.0,
                'weight_unit': '',
            }
            for p in products
        ]
        return request.make_response(
            json.dumps(out, default=str),
            headers=[('Content-Type', 'application/json')],
        )

    @http.route(
        '/promos/conflicts',
        type='http',
        auth='public',
        methods=['POST'],
        csrf=False,
    )
    def check_conflicts(self, **_):
        """Return existing approved submissions + plotted PTL deals that overlap.

        Body: JSON with keys
            brand_id (int)
            product_category (str)
            weight_value (float, optional)
            weight_unit (str, optional)
            market_ids (list[int])
            dates (list[str YYYY-MM-DD])

        Returns: JSON list of conflict entries
            [{date, source, deal_id, deal_name, vendor_name, market_id}]
        """
        try:
            payload = json.loads(request.httprequest.data or '{}')
        except (ValueError, TypeError):
            return request.make_response(
                json.dumps({'error': 'invalid json body'}),
                headers=[('Content-Type', 'application/json')],
                status=400,
            )

        brand_id = payload.get('brand_id')
        product_category = (payload.get('product_category') or '').strip()
        market_ids = payload.get('market_ids') or []
        dates = payload.get('dates') or []

        if not brand_id or not product_category or not market_ids or not dates:
            return request.make_response(
                json.dumps([]),
                headers=[('Content-Type', 'application/json')],
            )

        # Validate inputs defensively
        try:
            brand_id = int(brand_id)
            market_ids = [int(m) for m in market_ids]
        except (ValueError, TypeError):
            return request.make_response(
                json.dumps({'error': 'malformed ids'}),
                headers=[('Content-Type', 'application/json')],
                status=400,
            )
        valid_dates = []
        for d in dates:
            try:
                datetime.strptime(d, '%Y-%m-%d')
                valid_dates.append(d)
            except (ValueError, TypeError):
                continue
        if not valid_dates:
            return request.make_response(
                json.dumps([]),
                headers=[('Content-Type', 'application/json')],
            )

        weight_value = payload.get('weight_value')
        weight_unit = (payload.get('weight_unit') or '').strip() or None

        conflicts = []

        # 1) Plotted PTL deals: reuse the wizard's (date × market) × (brand_id,
        # product_category) shape via mint.ptl.day → deal_ids.
        Day = request.env['mint.ptl.day'].sudo()
        days = Day.search([
            ('date', 'in', valid_dates),
            ('market_id', 'in', market_ids),
        ])
        for day in days:
            for deal in day.deal_ids:
                if deal.brand_id.id != brand_id:
                    continue
                if (deal.product_category or '') != product_category:
                    continue
                if weight_unit is not None and weight_value:
                    if (deal.weight_unit or '') != weight_unit:
                        continue
                    if float(deal.weight_value or 0) != float(weight_value):
                        continue
                conflicts.append({
                    'date': day.date.strftime('%Y-%m-%d'),
                    'source': 'ptl',
                    'deal_id': deal.id,
                    'deal_name': deal.name,
                    'vendor_name': '',
                    'market_id': day.market_id.id,
                })

        # 2) Approved-but-not-yet-plotted submissions for the same brand ×
        # category + overlapping preferred date window.
        Submission = request.env['mint.deal.submission'].sudo()
        subs = Submission.search([
            ('state', '=', 'approved'),
            ('brand_id', '=', brand_id),
            ('product_category', '=', product_category),
        ])
        for sub in subs:
            sub_markets = set(sub.market_ids.ids) or ({sub.market_id.id} if sub.market_id else set())
            if not (sub_markets & set(market_ids)):
                continue
            for date_str in valid_dates:
                d = datetime.strptime(date_str, '%Y-%m-%d').date()
                in_window = (
                    sub.preferred_start_date
                    and sub.preferred_end_date
                    and sub.preferred_start_date <= d <= sub.preferred_end_date
                )
                in_picks = any(p.date == d for p in sub.plot_date_ids)
                if not (in_window or in_picks):
                    continue
                if weight_unit is not None and weight_value:
                    if (sub.weight_unit or '') != weight_unit:
                        continue
                    if float(sub.weight_value or 0) != float(weight_value):
                        continue
                conflicts.append({
                    'date': date_str,
                    'source': 'submission',
                    'deal_id': sub.id,
                    'deal_name': sub.name,
                    'vendor_name': sub.vendor_name,
                    'market_id': sub.market_id.id if sub.market_id else 0,
                })

        return request.make_response(
            json.dumps(conflicts, default=str),
            headers=[('Content-Type', 'application/json')],
        )
