import hmac
import json
import logging

from odoo import http, fields
from odoo.http import request, Response

_logger = logging.getLogger(__name__)


def _json(data, status=200):
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
    """Gate the inventory-ops API on the shared checkout API key (Odoo #662).
    Previously every route was auth=none with no key check -> unauthenticated
    read AND mutation of METRC-tracked stock (create/submit/approve/apply)."""
    key = request.httprequest.headers.get("X-Api-Key", "")
    if not key:
        return False
    expected = request.env["ir.config_parameter"].sudo().get_param(
        "mint_customer_api.checkout_api_key", ""
    )
    # Constant-time compare to avoid a timing side-channel (Odoo #675).
    return bool(key and expected and hmac.compare_digest(key, expected))


class InventoryAPI(http.Controller):

    # ------------------------------------------------------------------
    # CORS preflight
    # ------------------------------------------------------------------

    @http.route('/api/v1/inventory/<path:subpath>', type='http', auth='none',
                methods=['OPTIONS'], csrf=False)
    def preflight(self, subpath=None, **kw):
        return Response('', status=204, headers={
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'GET, POST, PUT, DELETE, OPTIONS',
            'Access-Control-Allow-Headers': 'Content-Type, X-Api-Key',
            'Access-Control-Max-Age': '86400',
        })

    # ------------------------------------------------------------------
    # Adjustments CRUD
    # ------------------------------------------------------------------

    @http.route('/api/v1/inventory/adjustments', type='http', auth='none',
                methods=['GET'], csrf=False)
    def list_adjustments(self, **kw):
        if not _verify_api_key():
            return _error("Unauthorized", 401)
        Adj = request.env['mint.inventory.adjustment'].sudo()
        domain = []

        if kw.get('company_id'):
            domain.append(('company_id', '=', int(kw['company_id'])))
        if kw.get('state'):
            states = kw['state'].split(',')
            domain.append(('state', 'in', states))
        if kw.get('type'):
            domain.append(('adjustment_type', '=', kw['type']))
        if kw.get('date_from'):
            domain.append(('create_date', '>=', kw['date_from']))
        if kw.get('date_to'):
            domain.append(('create_date', '<=', kw['date_to']))

        limit = min(int(kw.get('limit', 50)), 200)
        offset = int(kw.get('offset', 0))

        adjustments = Adj.search(domain, limit=limit, offset=offset, order='create_date desc')
        total = Adj.search_count(domain)

        return _json({
            'adjustments': [a._to_api_dict() for a in adjustments],
            'total': total,
            'limit': limit,
            'offset': offset,
        })

    @http.route('/api/v1/inventory/adjustments', type='http', auth='none',
                methods=['POST'], csrf=False)
    def create_adjustment(self, **kw):
        if not _verify_api_key():
            return _error("Unauthorized", 401)
        try:
            data = json.loads(request.httprequest.data or '{}')
        except json.JSONDecodeError:
            return _error('Invalid JSON', 400)

        required = ['adjustment_type', 'company_id', 'warehouse_id', 'lines']
        for field in required:
            if field not in data:
                return _error(f'Missing required field: {field}', 400)

        if not data['lines']:
            return _error('At least one line is required', 400)

        vals = {
            'adjustment_type': data['adjustment_type'],
            'company_id': int(data['company_id']),
            'warehouse_id': int(data['warehouse_id']),
            'reason': data.get('reason', ''),
            'metrc_tag': data.get('metrc_tag', ''),
            'batch_number': data.get('batch_number', ''),
        }

        if data.get('dest_warehouse_id'):
            vals['dest_warehouse_id'] = int(data['dest_warehouse_id'])

        line_vals = []
        for line in data['lines']:
            lv = {
                'product_id': int(line['product_id']),
            }
            if 'new_qty' in line:
                lv['new_qty'] = float(line['new_qty'])
            if 'quantity' in line:
                lv['quantity'] = float(line['quantity'])
            if line.get('lot_id'):
                lv['lot_id'] = int(line['lot_id'])
            if line.get('thc'):
                lv['thc'] = line['thc']
            if line.get('cbd'):
                lv['cbd'] = line['cbd']
            if line.get('metrc_tag'):
                lv['metrc_tag'] = line['metrc_tag']
            if line.get('notes'):
                lv['notes'] = line['notes']
            line_vals.append((0, 0, lv))

        vals['line_ids'] = line_vals

        try:
            adj = request.env['mint.inventory.adjustment'].sudo().create(vals)
            return _json({'success': True, 'id': adj.id, 'name': adj.name}, 201)
        except Exception as e:
            _logger.exception("Failed to create adjustment")
            return _error(str(e), 500)

    @http.route('/api/v1/inventory/adjustments/<int:adj_id>', type='http',
                auth='none', methods=['GET'], csrf=False)
    def get_adjustment(self, adj_id, **kw):
        if not _verify_api_key():
            return _error("Unauthorized", 401)
        adj = request.env['mint.inventory.adjustment'].sudo().browse(adj_id)
        if not adj.exists():
            return _error('Adjustment not found', 404)
        return _json(adj._to_api_dict())

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    @http.route('/api/v1/inventory/adjustments/<int:adj_id>/submit', type='http',
                auth='none', methods=['PUT'], csrf=False)
    def submit_adjustment(self, adj_id, **kw):
        if not _verify_api_key():
            return _error("Unauthorized", 401)
        adj = request.env['mint.inventory.adjustment'].sudo().browse(adj_id)
        if not adj.exists():
            return _error('Adjustment not found', 404)
        try:
            adj.action_submit()
            return _json({'success': True, 'state': adj.state})
        except Exception as e:
            return _error(str(e), 400)

    @http.route('/api/v1/inventory/adjustments/<int:adj_id>/approve', type='http',
                auth='none', methods=['PUT'], csrf=False)
    def approve_adjustment(self, adj_id, **kw):
        if not _verify_api_key():
            return _error("Unauthorized", 401)
        adj = request.env['mint.inventory.adjustment'].sudo().browse(adj_id)
        if not adj.exists():
            return _error('Adjustment not found', 404)
        try:
            adj.action_approve()
            return _json({'success': True, 'state': adj.state})
        except Exception as e:
            return _error(str(e), 400)

    @http.route('/api/v1/inventory/adjustments/<int:adj_id>/reject', type='http',
                auth='none', methods=['PUT'], csrf=False)
    def reject_adjustment(self, adj_id, **kw):
        if not _verify_api_key():
            return _error("Unauthorized", 401)
        adj = request.env['mint.inventory.adjustment'].sudo().browse(adj_id)
        if not adj.exists():
            return _error('Adjustment not found', 404)
        try:
            data = json.loads(request.httprequest.data or '{}')
            adj.action_do_reject(data.get('reason', ''))
            return _json({'success': True, 'state': adj.state})
        except Exception as e:
            return _error(str(e), 400)

    @http.route('/api/v1/inventory/adjustments/<int:adj_id>/apply', type='http',
                auth='none', methods=['PUT'], csrf=False)
    def apply_adjustment(self, adj_id, **kw):
        if not _verify_api_key():
            return _error("Unauthorized", 401)
        adj = request.env['mint.inventory.adjustment'].sudo().browse(adj_id)
        if not adj.exists():
            return _error('Adjustment not found', 404)
        try:
            adj.action_apply()
            return _json({'success': True, 'state': adj.state, 'picking_id': adj.picking_id.id or None})
        except Exception as e:
            _logger.exception("Failed to apply adjustment %s", adj.name)
            return _error(str(e), 500)

    # ------------------------------------------------------------------
    # Stock query
    # ------------------------------------------------------------------

    @http.route('/api/v1/inventory/stock', type='http', auth='none',
                methods=['GET'], csrf=False)
    def get_stock(self, **kw):
        if not _verify_api_key():
            return _error("Unauthorized", 401)
        domain = [('location_id.usage', '=', 'internal')]

        if kw.get('warehouse_id'):
            wh = request.env['stock.warehouse'].sudo().browse(int(kw['warehouse_id']))
            if wh.exists():
                domain.append(('location_id', '=', wh.lot_stock_id.id))

        if kw.get('product_id'):
            domain.append(('product_id', '=', int(kw['product_id'])))

        if kw.get('company_id'):
            domain.append(('company_id', '=', int(kw['company_id'])))

        limit = min(int(kw.get('limit', 100)), 500)
        offset = int(kw.get('offset', 0))

        quants = request.env['stock.quant'].sudo().search(
            domain, limit=limit, offset=offset,
            order='product_id',
        )

        result = []
        for q in quants:
            result.append({
                'product_id': q.product_id.id,
                'product_name': q.product_id.name,
                'sku': q.product_id.default_code or '',
                'quantity': q.quantity,
                'location_id': q.location_id.id,
                'location_name': q.location_id.complete_name,
                'warehouse_id': q.warehouse_id.id if q.warehouse_id else None,
                'company_id': q.company_id.id if q.company_id else None,
            })

        return _json({'stock': result, 'count': len(result), 'limit': limit, 'offset': offset})

    # ------------------------------------------------------------------
    # Shortcuts
    # ------------------------------------------------------------------

    @http.route('/api/v1/inventory/transfer', type='http', auth='none',
                methods=['POST'], csrf=False)
    def create_transfer(self, **kw):
        if not _verify_api_key():
            return _error("Unauthorized", 401)
        """Shortcut: create + submit a move adjustment."""
        try:
            data = json.loads(request.httprequest.data or '{}')
        except json.JSONDecodeError:
            return _error('Invalid JSON', 400)

        data['adjustment_type'] = 'move'
        request.httprequest._cached_data = json.dumps(data).encode()
        return self.create_adjustment(**kw)

    @http.route('/api/v1/inventory/destroy', type='http', auth='none',
                methods=['POST'], csrf=False)
    def create_destroy(self, **kw):
        if not _verify_api_key():
            return _error("Unauthorized", 401)
        """Shortcut: create + submit a destroy adjustment."""
        try:
            data = json.loads(request.httprequest.data or '{}')
        except json.JSONDecodeError:
            return _error('Invalid JSON', 400)

        if not data.get('reason'):
            return _error('Reason is required for destroy operations', 400)

        data['adjustment_type'] = 'destroy'
        request.httprequest._cached_data = json.dumps(data).encode()
        return self.create_adjustment(**kw)

    @http.route('/api/v1/inventory/batch-update', type='http', auth='none',
                methods=['POST'], csrf=False)
    def batch_update(self, **kw):
        if not _verify_api_key():
            return _error("Unauthorized", 401)
        """Batch update product inventory status or quantities."""
        try:
            data = json.loads(request.httprequest.data or '{}')
        except json.JSONDecodeError:
            return _error('Invalid JSON', 400)

        updates = data.get('updates', [])
        if not updates:
            return _error('No updates provided', 400)

        results = {'updated': 0, 'errors': []}
        Product = request.env['product.template'].sudo()

        for upd in updates:
            try:
                product = Product.browse(int(upd['product_id']))
                if not product.exists():
                    results['errors'].append({'product_id': upd['product_id'], 'error': 'Not found'})
                    continue

                vals = {}
                if 'inventory_status' in upd:
                    vals['inventory_status'] = upd['inventory_status']
                if 'discontinue_reason' in upd:
                    vals['discontinue_reason'] = upd['discontinue_reason']

                if vals:
                    product.write(vals)
                    results['updated'] += 1
            except Exception as e:
                results['errors'].append({'product_id': upd.get('product_id'), 'error': str(e)})

        return _json(results)
