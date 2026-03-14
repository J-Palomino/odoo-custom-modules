# -*- coding: utf-8 -*-
"""
POS Order REST API for BullMQ sync and frontend checkout.

Endpoints:
  POST   /api/v1/pos/orders           — Create order
  GET    /api/v1/pos/orders           — List orders (filter by store, state, date)
  PUT    /api/v1/pos/orders/<id>/state — Update order state
  POST   /api/v1/pos/orders/bulk-sync — Bulk upsert from BullMQ order-sync

Auth: X-Api-Key header (reuses mint_customer_api.checkout_api_key).
"""
import json
import logging
from datetime import datetime, timedelta

from odoo import http, fields
from odoo.http import request, Response

_logger = logging.getLogger(__name__)


def _json(data, status=200):
    """JSON response with CORS headers."""
    return Response(
        json.dumps(data, default=str),
        status=status,
        content_type='application/json',
        headers={
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'GET, POST, PUT, OPTIONS',
            'Access-Control-Allow-Headers': 'Content-Type, X-Api-Key',
        },
    )


def _error(message, status=400):
    return _json({'error': message}, status=status)


def _verify_api_key():
    """Verify X-Api-Key header matches the configured checkout API key."""
    key = request.httprequest.headers.get('X-Api-Key', '')
    if not key:
        return False
    expected = request.env['ir.config_parameter'].sudo().get_param(
        'mint_customer_api.checkout_api_key', ''
    )
    return key and expected and key == expected


def _normalize_phone(phone):
    """Strip phone to digits only, return last 10."""
    if not phone:
        return ''
    digits = ''.join(c for c in str(phone) if c.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits


def _find_or_create_partner(customer_data):
    """Find or create a res.partner from customer data dict."""
    if not customer_data:
        return None

    phone = _normalize_phone(customer_data.get('phone', ''))
    email = (customer_data.get('email') or '').strip().lower()

    Partner = request.env['res.partner'].sudo()
    partner = None

    if phone:
        partner = Partner.search([('phone', 'ilike', phone[-10:])], limit=1)
    if not partner and email:
        partner = Partner.search([('email', '=ilike', email)], limit=1)

    if not partner and (phone or email):
        name = ' '.join(filter(None, [
            customer_data.get('first_name', ''),
            customer_data.get('last_name', ''),
        ])).strip() or email or phone
        partner = Partner.create({
            'name': name,
            'email': email or False,
            'phone': phone or False,
            'customer_rank': 1,
        })
        _logger.info('Created new customer partner %s: %s', partner.id, name)

    return partner


ORDER_TYPE_MAP = {
    'PICKUP': 'pickup',
    'CURBSIDE_PICKUP': 'pickup',
    'DRIVE_THRU_PICKUP': 'pickup',
    'IN_STORE_PICKUP': 'in_store',
    'IN_STORE': 'in_store',
    'KIOSK': 'in_store',
    'DELIVERY': 'delivery',
    'pickup': 'pickup',
    'delivery': 'delivery',
    'in_store': 'in_store',
}


class MintPosOrderAPI(http.Controller):

    # ── CORS preflight ────────────────────────────────────────────────

    @http.route([
        '/api/v1/pos/orders',
        '/api/v1/pos/orders/<int:order_id>/state',
        '/api/v1/pos/orders/bulk-sync',
    ], type='http', auth='none', methods=['OPTIONS'], csrf=False)
    def preflight(self, **kw):
        return request.make_response('', headers=[
            ('Access-Control-Allow-Origin', '*'),
            ('Access-Control-Allow-Methods', 'GET, POST, PUT, OPTIONS'),
            ('Access-Control-Allow-Headers', 'Content-Type, X-Api-Key'),
            ('Access-Control-Max-Age', '86400'),
        ])

    # ── POST /api/v1/pos/orders — Create a single order ──────────────

    @http.route('/api/v1/pos/orders', type='http', auth='none',
                methods=['POST'], csrf=False, cors='*')
    def create_order(self, **kw):
        if not _verify_api_key():
            return _error('Invalid API key', 401)

        try:
            data = json.loads(request.httprequest.data)
        except (json.JSONDecodeError, TypeError):
            return _error('Invalid JSON body')

        items = data.get('items', [])
        if not items:
            return _error('Items are required')

        # Resolve store
        company = self._resolve_company(data)
        if not company:
            return _error('Store not found', 404)

        # Resolve customer
        partner = _find_or_create_partner(data.get('customer'))

        # Map order type
        raw_type = data.get('order_type', 'pickup')
        order_type = ORDER_TYPE_MAP.get(raw_type, 'pickup')

        # Build order values
        totals = data.get('totals', {})
        order_vals = {
            'partner_id': partner.id if partner else False,
            'company_id': company.id,
            'dutchie_checkout_id': data.get('dutchie_checkout_id', ''),
            'dutchie_receipt_no': data.get('dutchie_receipt_no', ''),
            'state': data.get('state', 'placed'),
            'order_type': order_type,
            'payment_method': data.get('payment_method', 'online'),
            'subtotal': totals.get('subtotal', 0),
            'discount_total': totals.get('discounts', 0),
            'tax_total': totals.get('taxes', 0),
            'total': totals.get('total', 0),
            'notes': data.get('notes', ''),
            'loyalty_points_earned': data.get('loyalty_points_earned', 0),
            'loyalty_points_redeemed': data.get('loyalty_points_redeemed', 0),
        }

        if data.get('placed_at'):
            order_vals['placed_at'] = data['placed_at']

        # Create order
        Order = request.env['mint.pos.order'].sudo().with_company(company)
        order = Order.create(order_vals)

        # Create line items
        Line = request.env['mint.pos.order.line'].sudo()
        for item in items:
            Line.create({
                'order_id': order.id,
                'product_name': item.get('product_name', 'Product'),
                'dutchie_product_id': item.get('dutchie_product_id', ''),
                'sku': item.get('sku', ''),
                'quantity': item.get('quantity', 1),
                'unit_price': item.get('unit_price', 0),
                'discount': item.get('discount', 0),
                'tax': item.get('tax', 0),
                'category': item.get('category', ''),
                'brand': item.get('brand', ''),
                'strain_type': item.get('strain_type', ''),
                'weight': item.get('weight', ''),
            })

        _logger.info(
            'POS order %s created for store %s (%d items, $%.2f)',
            order.name, company.name, len(items), order.total,
        )

        return _json({
            'success': True,
            'order_id': order.id,
            'order_ref': order.name,
            'state': order.state,
        })

    # ── GET /api/v1/pos/orders — List orders ─────────────────────────

    @http.route('/api/v1/pos/orders', type='http', auth='none',
                methods=['GET'], csrf=False, cors='*')
    def list_orders(self, **kw):
        if not _verify_api_key():
            return _error('Invalid API key', 401)

        domain = []

        # Filter by store (company_id or store_slug)
        if kw.get('company_id'):
            domain.append(('company_id', '=', int(kw['company_id'])))
        elif kw.get('store_slug'):
            company = request.env['res.company'].sudo().search(
                [('x_slug', '=', kw['store_slug'])], limit=1,
            )
            if company:
                domain.append(('company_id', '=', company.id))

        # Filter by state
        if kw.get('state'):
            states = kw['state'].split(',')
            domain.append(('state', 'in', states))

        # Filter by date range
        if kw.get('date_from'):
            domain.append(('placed_at', '>=', kw['date_from']))
        if kw.get('date_to'):
            domain.append(('placed_at', '<=', kw['date_to']))

        # Filter by dutchie_checkout_id
        if kw.get('dutchie_checkout_id'):
            domain.append(('dutchie_checkout_id', '=', kw['dutchie_checkout_id']))

        # Filter by phone (customer lookup)
        if kw.get('phone'):
            phone = _normalize_phone(kw['phone'])
            if phone:
                partners = request.env['res.partner'].sudo().search(
                    [('phone', 'ilike', phone[-10:])]
                )
                if partners:
                    domain.append(('partner_id', 'in', partners.ids))
                else:
                    return _json({'orders': [], 'total': 0, 'limit': 0, 'offset': 0})

        # Filter by email
        if kw.get('email'):
            email = kw['email'].strip().lower()
            partners = request.env['res.partner'].sudo().search(
                [('email', '=ilike', email)]
            )
            if partners:
                domain.append(('partner_id', 'in', partners.ids))
            else:
                return _json({'orders': [], 'total': 0, 'limit': 0, 'offset': 0})

        # Filter by order ref (MINT-POS-XXXXX)
        if kw.get('order_ref'):
            domain.append(('name', '=', kw['order_ref']))

        limit = min(int(kw.get('limit', 50)), 200)
        offset = int(kw.get('offset', 0))

        orders = request.env['mint.pos.order'].sudo().search(
            domain, limit=limit, offset=offset, order='placed_at desc',
        )
        total = request.env['mint.pos.order'].sudo().search_count(domain)

        return _json({
            'orders': [self._serialize_order(o) for o in orders],
            'total': total,
            'limit': limit,
            'offset': offset,
        })

    # ── PUT /api/v1/pos/orders/<id>/state — Update state ─────────────

    @http.route('/api/v1/pos/orders/<int:order_id>/state', type='http',
                auth='none', methods=['PUT'], csrf=False, cors='*')
    def update_state(self, order_id, **kw):
        if not _verify_api_key():
            return _error('Invalid API key', 401)

        try:
            data = json.loads(request.httprequest.data)
        except (json.JSONDecodeError, TypeError):
            return _error('Invalid JSON body')

        new_state = data.get('state')
        if not new_state:
            return _error('state is required')

        valid_states = [s[0] for s in request.env['mint.pos.order']._fields['state'].selection]
        if new_state not in valid_states:
            return _error(f'Invalid state: {new_state}')

        order = request.env['mint.pos.order'].sudo().browse(order_id)
        if not order.exists():
            return _error('Order not found', 404)

        vals = {'state': new_state}
        if data.get('budtender_id'):
            vals['budtender_id'] = data['budtender_id']
        if data.get('dutchie_receipt_no'):
            vals['dutchie_receipt_no'] = data['dutchie_receipt_no']

        order.write(vals)

        _logger.info('POS order %s state → %s', order.name, new_state)

        return _json({
            'success': True,
            'order_id': order.id,
            'order_ref': order.name,
            'state': order.state,
        })

    # ── POST /api/v1/pos/orders/bulk-sync — Batch upsert ─────────────

    @http.route('/api/v1/pos/orders/bulk-sync', type='http', auth='none',
                methods=['POST'], csrf=False, cors='*')
    def bulk_sync(self, **kw):
        """Bulk upsert orders from BullMQ order-sync job.

        Request body:
            {
              "orders": [
                {
                  "dutchie_receipt_no": "R-12345",
                  "company_id": 5,
                  "state": "completed",
                  "order_type": "in_store",
                  "payment_method": "cash",
                  "total": 45.00,
                  "placed_at": "2026-03-13T10:30:00",
                  "items": [ ... ]
                }
              ]
            }
        """
        if not _verify_api_key():
            return _error('Invalid API key', 401)

        try:
            data = json.loads(request.httprequest.data)
        except (json.JSONDecodeError, TypeError):
            return _error('Invalid JSON body')

        orders_data = data.get('orders', [])
        if not orders_data:
            return _json({'created': 0, 'updated': 0, 'errors': 0})

        Order = request.env['mint.pos.order'].sudo()
        Line = request.env['mint.pos.order.line'].sudo()

        created = 0
        updated = 0
        errors = 0

        for order_data in orders_data:
            try:
                company_id = order_data.get('company_id')
                receipt_no = order_data.get('dutchie_receipt_no', '')
                checkout_id = order_data.get('dutchie_checkout_id', '')

                # Try to find existing order
                existing = None
                if receipt_no and company_id:
                    existing = Order.search([
                        ('dutchie_receipt_no', '=', receipt_no),
                        ('company_id', '=', company_id),
                    ], limit=1)
                if not existing and checkout_id and company_id:
                    existing = Order.search([
                        ('dutchie_checkout_id', '=', checkout_id),
                        ('company_id', '=', company_id),
                    ], limit=1)

                if existing:
                    # Update existing order
                    update_vals = {}
                    new_state = order_data.get('state')
                    if new_state and new_state != existing.state:
                        update_vals['state'] = new_state
                    if receipt_no and not existing.dutchie_receipt_no:
                        update_vals['dutchie_receipt_no'] = receipt_no
                    if update_vals:
                        existing.write(update_vals)
                        updated += 1
                else:
                    # Create new order (walk-in transaction from Dutchie)
                    partner = _find_or_create_partner(order_data.get('customer'))
                    raw_type = order_data.get('order_type', 'in_store')
                    order_type = ORDER_TYPE_MAP.get(raw_type, 'in_store')

                    totals = order_data.get('totals', {})
                    order_vals = {
                        'partner_id': partner.id if partner else False,
                        'company_id': company_id,
                        'dutchie_checkout_id': checkout_id or False,
                        'dutchie_receipt_no': receipt_no or False,
                        'state': order_data.get('state', 'completed'),
                        'order_type': order_type,
                        'payment_method': order_data.get('payment_method', 'cash'),
                        'subtotal': totals.get('subtotal', order_data.get('subtotal', 0)),
                        'discount_total': totals.get('discounts', order_data.get('discount_total', 0)),
                        'tax_total': totals.get('taxes', order_data.get('tax_total', 0)),
                        'total': totals.get('total', order_data.get('total', 0)),
                        'notes': order_data.get('notes', ''),
                    }
                    if order_data.get('placed_at'):
                        order_vals['placed_at'] = order_data['placed_at']

                    order = Order.with_company(
                        request.env['res.company'].browse(company_id)
                    ).create(order_vals)

                    # Create line items
                    for item in order_data.get('items', []):
                        Line.create({
                            'order_id': order.id,
                            'product_name': item.get('product_name', 'Product'),
                            'dutchie_product_id': item.get('dutchie_product_id', ''),
                            'sku': item.get('sku', ''),
                            'quantity': item.get('quantity', 1),
                            'unit_price': item.get('unit_price', 0),
                            'discount': item.get('discount', 0),
                            'tax': item.get('tax', 0),
                            'category': item.get('category', ''),
                            'brand': item.get('brand', ''),
                            'strain_type': item.get('strain_type', ''),
                            'weight': item.get('weight', ''),
                        })

                    created += 1

            except Exception:
                _logger.exception(
                    'Error syncing order receipt=%s checkout=%s',
                    order_data.get('dutchie_receipt_no'),
                    order_data.get('dutchie_checkout_id'),
                )
                errors += 1

        _logger.info(
            'Bulk sync complete: %d created, %d updated, %d errors',
            created, updated, errors,
        )

        return _json({
            'created': created,
            'updated': updated,
            'errors': errors,
        })

    # ── Helpers ───────────────────────────────────────────────────────

    def _resolve_company(self, data):
        """Resolve res.company from data (company_id, store_slug, or store_id)."""
        Company = request.env['res.company'].sudo()

        if data.get('company_id'):
            return Company.browse(data['company_id']).exists()
        if data.get('store_slug'):
            return Company.search([('x_slug', '=', data['store_slug'])], limit=1)
        if data.get('store_id'):
            return Company.browse(data['store_id']).exists()

        return request.env.company

    def _serialize_order(self, order):
        """Serialize a mint.pos.order to a JSON-safe dict."""
        return {
            'id': order.id,
            'name': order.name,
            'customer': {
                'id': order.partner_id.id,
                'name': order.partner_id.name,
                'phone': order.partner_id.phone or '',
                'email': order.partner_id.email or '',
            } if order.partner_id else None,
            'company_id': order.company_id.id,
            'store_name': order.company_id.name,
            'store_slug': getattr(order.company_id, 'x_slug', '') or '',
            'dutchie_checkout_id': order.dutchie_checkout_id or '',
            'dutchie_receipt_no': order.dutchie_receipt_no or '',
            'state': order.state,
            'order_type': order.order_type,
            'payment_method': order.payment_method,
            'subtotal': order.subtotal,
            'discount_total': order.discount_total,
            'tax_total': order.tax_total,
            'total': order.total,
            'line_count': order.line_count,
            'placed_at': order.placed_at,
            'confirmed_at': order.confirmed_at,
            'ready_at': order.ready_at,
            'completed_at': order.completed_at,
            'budtender': order.budtender_id.name if order.budtender_id else None,
            'notes': order.notes or '',
            'items': [{
                'product_name': l.product_name,
                'sku': l.sku or '',
                'quantity': l.quantity,
                'unit_price': l.unit_price,
                'discount': l.discount,
                'line_total': l.line_total,
                'category': l.category or '',
                'brand': l.brand or '',
            } for l in order.line_ids],
        }
