# -*- coding: utf-8 -*-
"""
Cart endpoints. Fleshed out in Task #5.

  GET    /api/v1/cart?store_id=X
  PUT    /api/v1/cart
  DELETE /api/v1/cart?store_id=X
  POST   /api/v1/cart/merge
"""
from odoo import http
from odoo.http import Response


@http.route(
    ['/api/v1/cart', '/api/v1/cart/merge'],
    type='http', auth='public', methods=['GET', 'PUT', 'POST', 'DELETE', 'OPTIONS'],
    csrf=False, cors='*',
)
def cart_stub(**kw):
    if http.request.httprequest.method == 'OPTIONS':
        return Response(status=204)
    return Response(
        '{"error":"not implemented"}',
        status=501, content_type='application/json',
    )
