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
        '/api/v1/pos/orders/stats',
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

        # Map payment method (frontend sends 'in-store', model expects 'cash')
        raw_pm = data.get('payment_method', 'online')
        pm_map = {'in-store': 'cash', 'pay-at-store': 'cash'}
        payment_method = pm_map.get(raw_pm, raw_pm) if raw_pm else 'online'

        # Build order values
        totals = data.get('totals', {})
        order_vals = {
            'partner_id': partner.id if partner else False,
            'company_id': company.id,
            'dutchie_checkout_id': data.get('dutchie_checkout_id', ''),
            'dutchie_receipt_no': data.get('dutchie_receipt_no', ''),
            'state': data.get('state', 'placed'),
            'order_type': order_type,
            'payment_method': payment_method,
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
        if data.get('is_prepaid'):
            order_vals['is_prepaid'] = True
        if data.get('dutchie_subtotal') is not None:
            order_vals['dutchie_subtotal'] = data['dutchie_subtotal']
        if data.get('dutchie_tax') is not None:
            order_vals['dutchie_tax'] = data['dutchie_tax']
        if data.get('dutchie_total') is not None:
            order_vals['dutchie_total'] = data['dutchie_total']
        if data.get('items_total'):
            order_vals['items_total'] = int(data['items_total'])
        if data.get('items_failed'):
            order_vals['items_failed'] = int(data['items_failed'])

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
            'partner_id': order.partner_id.id if order.partner_id else None,
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

        # Filter by dutchie_receipt_no (TransactionReference from POS)
        if kw.get('dutchie_receipt_no'):
            domain.append(('dutchie_receipt_no', '=', kw['dutchie_receipt_no']))

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

        # Filter by source (web, dutchie_sync, odoo_pos, walk_in)
        if kw.get('source'):
            domain.append(('source', '=', kw['source']))

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
        if data.get('is_prepaid') is not None:
            vals['is_prepaid'] = bool(data['is_prepaid'])
        if data.get('payment_confirmed_at'):
            vals['payment_confirmed_at'] = data['payment_confirmed_at']
        if data.get('dutchie_order_number'):
            vals['dutchie_order_number'] = data['dutchie_order_number']
        if data.get('dutchie_subtotal') is not None:
            vals['dutchie_subtotal'] = data['dutchie_subtotal']
        if data.get('dutchie_tax') is not None:
            vals['dutchie_tax'] = data['dutchie_tax']
        if data.get('dutchie_total') is not None:
            vals['dutchie_total'] = data['dutchie_total']

        order.write(vals)

        _logger.info('POS order %s state -> %s', order.name, new_state)

        # Send push notification directly from controller (belt + suspenders)
        if order.partner_id:
            try:
                msgs = order._get_notification_messages()
                msg = msgs.get(new_state)
                if msg:
                    store_name = order.company_id.name or ''
                    ref = order.name or ''
                    title = msg['title']
                    body = msg['body'].format(store=store_name, ref=ref)
                    order_url = '/orders?ref=%s' % ref

                    sent = request.env['mint.push.subscription'].sudo().send_to_partner(
                        partner_id=order.partner_id.id,
                        title=title,
                        body=body,
                        url=order_url,
                    )
                    _logger.info(
                        'Push [%s] sent for order %s to partner %s (%d delivered)',
                        new_state, order.name, order.partner_id.id, sent,
                    )
            except Exception:
                _logger.exception(
                    'Failed to send push for order %s', order.name,
                )

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
                    if order_data.get('dutchie_order_number') and not existing.dutchie_order_number:
                        update_vals['dutchie_order_number'] = order_data['dutchie_order_number']
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
                        'source': order_data.get('source', 'dutchie_sync'),
                    }
                    if order_data.get('dutchie_shipment_id'):
                        order_vals['dutchie_shipment_id'] = order_data['dutchie_shipment_id']
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
            'is_prepaid': order.is_prepaid,
            'dutchie_order_number': order.dutchie_order_number or '',
            'payment_confirmed_at': order.payment_confirmed_at,
            'dutchie_total': order.dutchie_total,
            'items_total': order.items_total,
            'items_failed': order.items_failed,
            'source': order.source or '',
            'dutchie_shipment_id': order.dutchie_shipment_id or '',
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

    # ── Web Order Config API ──────────────────────────────────

    @http.route('/api/v1/pos/web-order-config', type='http', auth='none',
                methods=['GET', 'OPTIONS'], csrf=False, cors='*')
    def get_web_order_config(self):
        """Get web order configuration for all stores (for BullMQ worker)."""
        if not _verify_api_key():
            return _json({'error': 'Forbidden'}, 403)

        ConfigAPI = request.env['mint.web.order.config.api'].sudo()
        configs = ConfigAPI.get_all_configs()
        return _json({'configs': configs})

    @http.route('/api/v1/pos/web-order-config/<string:store_slug>', type='http', auth='none',
                methods=['GET', 'OPTIONS'], csrf=False, cors='*')
    def get_web_order_config_for_store(self, store_slug):
        """Get web order configuration for a specific store."""
        if not _verify_api_key():
            return _json({'error': 'Forbidden'}, 403)

        ConfigAPI = request.env['mint.web.order.config.api'].sudo()
        config = ConfigAPI.get_config_for_store(store_slug)
        if not config:
            return _json({'error': 'No config for store: ' + store_slug}, 404)
        return _json(config)

    @http.route('/api/v1/pos/orders/cancel', type='http', auth='none',
                methods=['POST', 'OPTIONS'], csrf=False, cors='*')
    def cancel_order(self):
        """Cancel an order by shipment ID or order ref."""
        if not _verify_api_key():
            return _json({'error': 'Forbidden'}, 403)

        try:
            data = json.loads(request.httprequest.data)
        except (json.JSONDecodeError, TypeError):
            return _json({'error': 'Invalid JSON'}, 400)

        Order = request.env['mint.pos.order'].sudo()

        order = None
        if data.get('dutchie_shipment_id'):
            order = Order.search([
                ('dutchie_checkout_id', '=', str(data['dutchie_shipment_id']))
            ], limit=1)
        if not order and data.get('dutchie_receipt_no'):
            order = Order.search([
                ('dutchie_receipt_no', '=', str(data['dutchie_receipt_no']))
            ], limit=1)

        if not order:
            return _json({'error': 'Order not found'}, 404)

        order.write({
            'state': 'cancelled',
            'notes': (order.notes or '') + '\nCancelled: ' + (data.get('reason') or 'No reason'),
        })

        # Send push notification if configured
        if order.partner_id:
            config = request.env['mint.web.order.config'].sudo().search([
                ('company_id', '=', order.company_id.id),
                ('active', '=', True),
            ], limit=1)
            if config and config.auto_push_notifications:
                title, body = config.format_push_message(
                    'cancelled',
                    store_name=order.company_id.name,
                    order_ref=order.name,
                    customer_name=order.partner_id.name,
                )
                if title:
                    try:
                        request.env['mint.push.subscription'].sudo().send_to_partner(
                            order.partner_id.id, title, body,
                            url='/order/' + (order.name or ''),
                        )
                    except Exception as e:
                        _logger.warning('Push notification failed: %s', e)

        return _json({'success': True, 'order_id': order.id, 'state': 'cancelled'})

    # ── GET /api/v1/pos/orders/stats — Aggregate counts ────────────

    @http.route('/api/v1/pos/orders/stats', type='http', auth='none',
                methods=['GET'], csrf=False, cors='*')
    def order_stats(self, **kw):
        if not _verify_api_key():
            return _error('Invalid API key', 401)

        domain = []

        # Optional date filter (default: today)
        if kw.get('date_from'):
            domain.append(('placed_at', '>=', kw['date_from']))
        if kw.get('date_to'):
            domain.append(('placed_at', '<=', kw['date_to']))

        Order = request.env['mint.pos.order'].sudo()

        # read_group: count orders grouped by state and company_id
        groups = Order.read_group(
            domain,
            fields=['id'],
            groupby=['company_id', 'state'],
            lazy=False,
        )

        # Build response: list of {company_id, company_name, state, count}
        stats = []
        for g in groups:
            company = g.get('company_id')
            stats.append({
                'company_id': company[0] if company else None,
                'company_name': company[1] if company else 'Unknown',
                'state': g.get('state') or 'unknown',
                'count': g.get('__count', 0),
            })

        # Also include store metadata for label enrichment
        company_ids = list({s['company_id'] for s in stats if s['company_id']})
        stores = {}
        if company_ids:
            companies = request.env['res.company'].sudo().browse(company_ids)
            for c in companies:
                stores[c.id] = {
                    'name': c.name,
                    'slug': c.x_slug or '',
                    'state': (c.state_id.code if c.state_id else '') or '',
                    'city': c.city or '',
                }

        return _json({
            'stats': stats,
            'stores': stores,
            'total_orders': sum(s['count'] for s in stats),
        })
