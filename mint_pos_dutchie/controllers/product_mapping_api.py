# -*- coding: utf-8 -*-
"""
Product mapping lookup API.

Used by the BullMQ dutchiePosRelay to resolve Odoo product IDs to
per-store Dutchie POS Product IDs via mint.product.location.
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
            'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
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


class ProductMappingController(http.Controller):

    @http.route(
        '/api/v1/pos/product-mapping',
        type='http', auth='none', methods=['GET', 'OPTIONS'],
        csrf=False,
    )
    def get_mappings(self, **kw):
        """Look up Dutchie Product IDs for a list of product template IDs at a store.

        Query params:
            company_id: int — store company ID
            product_tmpl_ids: comma-separated product.template IDs
        """
        if request.httprequest.method == 'OPTIONS':
            return _json({})

        if not _verify_api_key():
            return _json({'error': 'Unauthorized'}, 401)

        company_id = kw.get('company_id')
        tmpl_ids_str = kw.get('product_tmpl_ids', '')

        if not company_id or not tmpl_ids_str:
            return _json({'error': 'company_id and product_tmpl_ids required'}, 400)

        try:
            tmpl_ids = [int(x) for x in tmpl_ids_str.split(',') if x.strip()]
        except ValueError:
            return _json({'error': 'product_tmpl_ids must be comma-separated integers'}, 400)

        mappings = request.env['mint.product.location'].sudo().search([
            ('company_id', '=', int(company_id)),
            ('product_tmpl_id', 'in', tmpl_ids),
            ('dutchie_product_id', '!=', False),
        ])

        result = {}
        for m in mappings:
            result[str(m.product_tmpl_id.id)] = {
                'dutchie_product_id': m.dutchie_product_id,
                'dutchie_plus_id': m.dutchie_plus_id or '',
                'barcode': m.barcode or '',
                'rec_price': m.rec_price,
                'quantity_available': m.quantity_available,
            }

        return _json({
            'company_id': int(company_id),
            'mappings': result,
            'found': len(result),
            'requested': len(tmpl_ids),
        })

    @http.route(
        '/api/v1/pos/product-mapping/bulk',
        type='http', auth='none', methods=['POST', 'OPTIONS'],
        csrf=False,
    )
    def bulk_upsert_mappings(self, **kw):
        """Bulk create/update product mappings from backfill or sync scripts.

        Body:
            {
                "company_id": 32,
                "mappings": [
                    {
                        "product_tmpl_id": 12345,
                        "dutchie_product_id": "12246434",
                        "dutchie_plus_id": "661944667e...",
                        "barcode": "05803748",
                        "rec_price": 15.00
                    }
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

        company_id = body.get('company_id')
        mappings_data = body.get('mappings', [])

        if not company_id or not mappings_data:
            return _json({'error': 'company_id and mappings required'}, 400)

        Mapping = request.env['mint.product.location'].sudo()
        created = 0
        updated = 0
        errors = 0

        for item in mappings_data:
            tmpl_id = item.get('product_tmpl_id')
            if not tmpl_id:
                errors += 1
                continue

            try:
                existing = Mapping.search([
                    ('product_tmpl_id', '=', tmpl_id),
                    ('company_id', '=', company_id),
                ], limit=1)

                vals = {
                    'product_tmpl_id': tmpl_id,
                    'company_id': company_id,
                }
                for field in ('dutchie_product_id', 'dutchie_plus_id',
                              'dutchie_package_id', 'barcode',
                              'rec_price', 'med_price'):
                    if field in item and item[field] is not None:
                        vals[field] = item[field]

                if existing:
                    existing.write(vals)
                    updated += 1
                else:
                    Mapping.create(vals)
                    created += 1
            except Exception:
                _logger.exception(
                    'Failed to upsert mapping for tmpl=%s company=%s',
                    tmpl_id, company_id,
                )
                errors += 1

        _logger.info(
            'Product mapping bulk upsert: %d created, %d updated, %d errors (company=%s)',
            created, updated, errors, company_id,
        )

        return _json({
            'created': created,
            'updated': updated,
            'errors': errors,
        })
