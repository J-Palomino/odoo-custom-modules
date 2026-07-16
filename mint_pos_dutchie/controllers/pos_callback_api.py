# -*- coding: utf-8 -*-
"""
Dutchie sync callback API.

Called by the inventory service after relaying a POS order to Dutchie.
Updates the pos.order with Dutchie receipt number and sync status.
"""
import json
import logging

from odoo import http
from odoo.http import request, Response

_logger = logging.getLogger(__name__)


def _json(data, status=200):
    return Response(
        json.dumps(data, default=str),
        status=status,
        content_type='application/json',
        headers={
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'PUT, OPTIONS',
            'Access-Control-Allow-Headers': 'Content-Type, X-Api-Key',
        },
    )


def _verify_api_key():
    key = request.httprequest.headers.get('X-Api-Key', '')
    if not key:
        return False
    expected = request.env['ir.config_parameter'].sudo().get_param(
        'mint_customer_api.checkout_api_key', '',
    )
    return key and expected and key == expected


class PosCallbackController(http.Controller):

    # ── CANONICAL OWNER: dutchie-status callback (Task #106611) ────────
    # This is the ONLY handler for PUT /api/v1/pos/orders/<id>/dutchie-status.
    # Called by mintinvsvc dutchiePosRelay.js with native pos.order IDs.
    # Duplicate in mint_pos_bridge was removed 2026-07-16.
    @http.route(
        '/api/v1/pos/orders/<int:order_id>/dutchie-status',
        type='http', auth='none', methods=['PUT', 'OPTIONS'],
        csrf=False,
    )
    def update_dutchie_status(self, order_id, **kwargs):
        """Update pos.order with Dutchie relay result."""
        if request.httprequest.method == 'OPTIONS':
            return _json({})

        if not _verify_api_key():
            return _json({'error': 'Unauthorized'}, 401)

        try:
            body = json.loads(request.httprequest.get_data(as_text=True))
        except (json.JSONDecodeError, TypeError):
            return _json({'error': 'Invalid JSON'}, 400)

        order = request.env['pos.order'].sudo().browse(order_id)
        if not order.exists():
            return _json({'error': 'Order not found'}, 404)

        vals = {}
        if body.get('success'):
            vals['dutchie_synced'] = True
            vals['dutchie_sync_error'] = False
            if body.get('dutchie_receipt_no'):
                vals['dutchie_receipt_no'] = body['dutchie_receipt_no']
            if body.get('dutchie_shipment_id'):
                vals['dutchie_shipment_id'] = body['dutchie_shipment_id']
        else:
            vals['dutchie_synced'] = False
            vals['dutchie_sync_error'] = body.get('error', 'Unknown error')

        order.write(vals)

        _logger.info(
            'Dutchie status updated for pos.order %s: synced=%s',
            order.name, vals.get('dutchie_synced'),
        )

        return _json({
            'success': True,
            'order_id': order.id,
            'order_ref': order.name,
            'dutchie_synced': vals.get('dutchie_synced', False),
        })
