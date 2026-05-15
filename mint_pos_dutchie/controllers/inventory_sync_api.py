# -*- coding: utf-8 -*-
"""
Inventory quantity sync API.

Receives bulk quantity updates from the inventory service and adjusts
Odoo stock quants to match Dutchie's live inventory.
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
            'Access-Control-Allow-Methods': 'POST, OPTIONS',
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


class InventorySyncController(http.Controller):

    @http.route(
        '/api/v1/pos/inventory/sync',
        type='http', auth='none', methods=['POST', 'OPTIONS'],
        csrf=False,
    )
    def sync_inventory(self, **kwargs):
        """Bulk update product quantities from Dutchie inventory.

        Body:
            {
                "company_id": 5,
                "updates": [
                    {"dutchie_product_id": "123", "quantity": 50},
                    ...
                ]
            }
        """
        if request.httprequest.method == 'OPTIONS':
            return _json({})

        if not _verify_api_key():
            return _json({'error': 'Unauthorized'}, 401)

        try:
            body = json.loads(request.httprequest.get_data(as_text=True))
        except (json.JSONDecodeError, TypeError):
            return _json({'error': 'Invalid JSON'}, 400)

        updates = body.get('updates', [])
        company_id = body.get('company_id')
        if not updates:
            return _json({'error': 'No updates provided'}, 400)

        Product = request.env['product.product'].sudo()
        StockQuant = request.env['stock.quant'].sudo()

        # Get default stock location for the company
        warehouse = request.env['stock.warehouse'].sudo().search(
            [('company_id', '=', company_id)] if company_id else [],
            limit=1,
        )
        if not warehouse:
            return _json({'error': 'No warehouse found'}, 400)
        stock_location = warehouse.lot_stock_id

        updated = 0
        skipped = 0
        errors = 0

        for item in updates:
            dutchie_id = item.get('dutchie_product_id')
            quantity = item.get('quantity', 0)
            if not dutchie_id:
                skipped += 1
                continue

            try:
                # Find product by dutchie_product_id on the template
                product = Product.search([
                    ('product_tmpl_id.dutchie_product_id', '=', str(dutchie_id)),
                ], limit=1)
                if not product:
                    skipped += 1
                    continue

                # Update quantity via stock quant
                quant = StockQuant.search([
                    ('product_id', '=', product.id),
                    ('location_id', '=', stock_location.id),
                ], limit=1)

                if quant:
                    quant.inventory_quantity = quantity
                    quant.action_apply_inventory()
                else:
                    StockQuant.create({
                        'product_id': product.id,
                        'location_id': stock_location.id,
                        'inventory_quantity': quantity,
                    }).action_apply_inventory()

                updated += 1
            except Exception:
                _logger.exception(
                    'Failed to sync quantity for dutchie_product_id=%s', dutchie_id,
                )
                errors += 1

        _logger.info(
            'Inventory sync: %d updated, %d skipped, %d errors (company=%s)',
            updated, skipped, errors, company_id,
        )

        return _json({
            'updated': updated,
            'skipped': skipped,
            'errors': errors,
        })
