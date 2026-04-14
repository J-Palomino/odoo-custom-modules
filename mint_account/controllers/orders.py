# -*- coding: utf-8 -*-
"""
Customer-facing order lookup. Read-only. Fleshed out in Task #5.

  GET /api/v1/orders?limit=20&offset=0
  GET /api/v1/orders/<ref>

These routes live under /api/v1/orders (distinct from mint_pos_bridge's
/api/v1/pos/orders, which is the sync-side admin API). Results are
filtered by the JWT-derived partner_id so no customer sees another's
orders.
"""
from odoo import http
from odoo.http import Response


@http.route(
    ['/api/v1/orders', '/api/v1/orders/<string:ref>'],
    type='http', auth='public', methods=['GET', 'OPTIONS'],
    csrf=False, cors='*',
)
def orders_stub(**kw):
    if http.request.httprequest.method == 'OPTIONS':
        return Response(status=204)
    return Response(
        '{"error":"not implemented"}',
        status=501, content_type='application/json',
    )
