# -*- coding: utf-8 -*-
"""
Customer cart endpoints. JWT-authenticated (Bearer via mint_customer_api).

  GET    /api/v1/cart?store_id=X        — returns {cart|null}
  PUT    /api/v1/cart                   — body: {store_id, items[]}
  DELETE /api/v1/cart?store_id=X        — clears cart for that store
  POST   /api/v1/cart/merge             — body: {store_id, items[]}; unions
                                           local items with server cart

Cart is keyed by (partner_id, company_id) for Florida-compliant per-region
isolation. A partner has one cart per store; moving stores does not destroy
the cart at the old store.
"""
import logging

from odoo import http
from odoo.http import request

from ._common import (
    require_partner,
    read_json_body,
    cors_preflight,
    json_response,
    error_response,
)

_logger = logging.getLogger(__name__)


def _serialize_cart(cart):
    """Shape the cart record for the FE. None-safe."""
    if not cart:
        return None
    return {
        'store_id': cart.company_id.id,
        'items': cart.items_list(),
        'updated_at': cart.updated_at.isoformat() if cart.updated_at else None,
    }


def _parse_store_id(raw):
    """Coerce store_id from query string / body to an int. Returns None on bad input."""
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _validate_items(items):
    """Light shape validation — each item must be a dict with a productId.

    We don't enforce Dutchie-side fields (unitPrice, maxQuantity, etc.) here
    because the cart mirrors the FE payload verbatim. The FE and checkout
    controllers are the source of truth for product pricing/availability;
    the cart is just a persistence layer.
    """
    if not isinstance(items, list):
        return 'items must be a JSON array'
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            return f'items[{i}] must be an object'
        if not it.get('productId'):
            return f'items[{i}].productId is required'
    return None


class MintCartController(http.Controller):

    @http.route(
        '/api/v1/cart', type='http', auth='none',
        methods=['GET', 'PUT', 'DELETE', 'OPTIONS'],
        csrf=False, cors='*',
    )
    def cart(self, **kw):
        if request.httprequest.method == 'OPTIONS':
            return cors_preflight()

        partner, err = require_partner()
        if err:
            return err

        method = request.httprequest.method
        if method == 'GET':
            return self._get(partner, kw)
        if method == 'PUT':
            return self._put(partner)
        if method == 'DELETE':
            return self._delete(partner, kw)
        return error_response('Method not allowed', 405)

    def _get(self, partner, kw):
        store_id = _parse_store_id(kw.get('store_id'))
        if not store_id:
            return error_response('store_id query param is required')
        cart = request.env['mint.cart'].sudo().search([
            ('partner_id', '=', partner.id),
            ('company_id', '=', store_id),
        ], limit=1)
        return json_response({'cart': _serialize_cart(cart)})

    def _put(self, partner):
        data, err = read_json_body()
        if err:
            return err
        store_id = _parse_store_id(data.get('store_id'))
        if not store_id:
            return error_response('store_id is required')

        items = data.get('items', [])
        shape_err = _validate_items(items)
        if shape_err:
            return error_response(shape_err)

        # Verify the store exists — guards against a malicious body that
        # references a company the partner has never seen.
        if not request.env['res.company'].sudo().browse(store_id).exists():
            return error_response('Unknown store_id', 404)

        cart = request.env['mint.cart'].sudo().get_or_create(partner.id, store_id)
        cart.set_items(items)
        return json_response({'cart': _serialize_cart(cart)})

    def _delete(self, partner, kw):
        store_id = _parse_store_id(kw.get('store_id'))
        if not store_id:
            return error_response('store_id query param is required')
        cart = request.env['mint.cart'].sudo().search([
            ('partner_id', '=', partner.id),
            ('company_id', '=', store_id),
        ], limit=1)
        if cart:
            cart.unlink()
        return json_response({'success': True})

    @http.route(
        '/api/v1/cart/merge', type='http', auth='none',
        methods=['POST', 'OPTIONS'], csrf=False, cors='*',
    )
    def merge(self, **kw):
        """Union FE's local cart with the server cart for this store.

        Used at login: FE POSTs whatever was sitting in localStorage, gets
        back the merged result, and drops the local copy. See
        mint.cart.merge_items for the dedupe rule (sum quantities, cap at
        maxQuantity, incoming metadata wins)."""
        if request.httprequest.method == 'OPTIONS':
            return cors_preflight()

        partner, err = require_partner()
        if err:
            return err

        data, err = read_json_body()
        if err:
            return err

        store_id = _parse_store_id(data.get('store_id'))
        if not store_id:
            return error_response('store_id is required')

        items = data.get('items', [])
        shape_err = _validate_items(items)
        if shape_err:
            return error_response(shape_err)

        if not request.env['res.company'].sudo().browse(store_id).exists():
            return error_response('Unknown store_id', 404)

        cart = request.env['mint.cart'].sudo().get_or_create(partner.id, store_id)
        cart.merge_items(items)
        return json_response({'cart': _serialize_cart(cart)})
