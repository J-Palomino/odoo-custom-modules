# -*- coding: utf-8 -*-
"""
Customer-facing order lookup. JWT-authenticated; results filtered by the
partner_id bound to the token so customers only see their own orders.

  GET /api/v1/orders?limit=20&offset=0&store_id=X
      — list the partner's mint.pos.order records (placed in last 90 days),
        newest first.
  GET /api/v1/orders/<ref>
      — single order detail (lines included). 404 if the order doesn't
        belong to the signed-in partner, even if the ref exists — we don't
        leak existence to other customers.

Reads from mint.pos.order (owned by mint_pos_bridge). The admin-scoped
sync API at /api/v1/pos/orders (X-Api-Key) is a different surface; this
one is for the customer "My Orders" view.
"""
import logging
from datetime import datetime, timedelta

from odoo import http
from odoo.http import request

from ._common import (
    require_partner,
    cors_preflight,
    json_response,
    error_response,
)

_logger = logging.getLogger(__name__)

ORDER_HISTORY_DAYS = 90
MAX_PAGE_SIZE = 50
DEFAULT_PAGE_SIZE = 20


def _serialize_order_summary(order):
    """Compact representation for the list view."""
    return {
        'id': order.id,
        'name': order.name,
        'state': order.state,
        'order_type': order.order_type,
        'payment_method': order.payment_method,
        'total': order.total,
        'subtotal': order.subtotal,
        'discount_total': order.discount_total,
        'tax_total': order.tax_total,
        'line_count': order.line_count,
        'placed_at': order.placed_at.isoformat() if order.placed_at else None,
        'completed_at': order.completed_at.isoformat() if order.completed_at else None,
        'store': {
            'id': order.company_id.id,
            'name': order.company_id.name,
        },
    }


def _serialize_order_detail(order):
    """Full order with line items."""
    data = _serialize_order_summary(order)
    data['lines'] = [{
        'product_name': line.product_name,
        'sku': line.sku or '',
        'quantity': line.quantity,
        'unit_price': line.unit_price,
        'discount': line.discount,
    } for line in order.line_ids]
    data['notes'] = order.notes or ''
    data['confirmed_at'] = order.confirmed_at.isoformat() if order.confirmed_at else None
    data['ready_at'] = order.ready_at.isoformat() if order.ready_at else None
    data['dutchie_receipt_no'] = order.dutchie_receipt_no or ''
    return data


def _parse_int(raw, default, maximum=None):
    """Coerce a query-string int with a default + optional cap."""
    try:
        n = int(raw) if raw is not None else default
    except (TypeError, ValueError):
        n = default
    if n < 0:
        n = default
    if maximum is not None and n > maximum:
        n = maximum
    return n


class MintOrdersController(http.Controller):

    @http.route(
        '/api/v1/orders', type='http', auth='none',
        methods=['GET', 'OPTIONS'], csrf=False, cors='*',
    )
    def list_orders(self, **kw):
        if request.httprequest.method == 'OPTIONS':
            return cors_preflight()

        partner, err = require_partner()
        if err:
            return err

        limit = _parse_int(kw.get('limit'), DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE)
        offset = _parse_int(kw.get('offset'), 0)

        cutoff = datetime.utcnow() - timedelta(days=ORDER_HISTORY_DAYS)
        domain = [
            ('partner_id', '=', partner.id),
            ('placed_at', '>=', cutoff),
        ]

        # Optional per-store filter. Useful for the FE to render "orders at
        # this store" on the store page — not required for the account view.
        store_id_raw = kw.get('store_id')
        if store_id_raw:
            try:
                domain.append(('company_id', '=', int(store_id_raw)))
            except (TypeError, ValueError):
                return error_response('Invalid store_id')

        Order = request.env['mint.pos.order'].sudo()
        total_count = Order.search_count(domain)
        orders = Order.search(domain, order='placed_at desc', limit=limit, offset=offset)

        return json_response({
            'orders': [_serialize_order_summary(o) for o in orders],
            'total': total_count,
            'limit': limit,
            'offset': offset,
        })

    @http.route(
        '/api/v1/orders/<string:ref>', type='http', auth='none',
        methods=['GET', 'OPTIONS'], csrf=False, cors='*',
    )
    def get_order(self, ref, **kw):
        if request.httprequest.method == 'OPTIONS':
            return cors_preflight()

        partner, err = require_partner()
        if err:
            return err

        # Look up by name (customer-facing ref like "MINT-POS-00037") OR
        # numeric id. We deliberately don't fall back to dutchie_receipt_no
        # because those aren't unique across stores.
        Order = request.env['mint.pos.order'].sudo()
        order = Order.search([
            ('partner_id', '=', partner.id),
            ('name', '=', ref),
        ], limit=1)
        if not order and ref.isdigit():
            order = Order.search([
                ('partner_id', '=', partner.id),
                ('id', '=', int(ref)),
            ], limit=1)

        if not order:
            # Don't leak whether the ref exists for a different partner —
            # always return 404.
            return error_response('Order not found', 404)

        return json_response({'order': _serialize_order_detail(order)})
