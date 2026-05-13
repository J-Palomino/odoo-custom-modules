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
from datetime import datetime

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


def _normalize_datetime(value):
    """Coerce various incoming datetime formats to Odoo's 'YYYY-MM-DD HH:MM:SS'.

    Accepts:
      - ISO 8601 with 'T' separator and optional 'Z' or ±HH:MM tz
        (e.g. '2026-04-17T19:45:00Z', '2026-04-17T19:45:00.123-07:00')
      - Odoo-native naive format (already correct)
      - datetime object

    Returns a naive UTC string Odoo can store. Returns None on unparseable input
    so the caller can fall back to the model default.
    """
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value).strip()
        # Odoo's native format — pass through
        try:
            dt = datetime.strptime(s, '%Y-%m-%d %H:%M:%S')
        except ValueError:
            # ISO 8601 — fromisoformat handles '2026-04-17T19:45:00+00:00'
            # and (on 3.11+) '2026-04-17T19:45:00Z'. Strip trailing Z defensively
            # for older Pythons and trim fractional seconds beyond microseconds.
            try:
                iso = s.replace('Z', '+00:00')
                dt = datetime.fromisoformat(iso)
            except ValueError:
                return None
    # Convert to naive UTC — Odoo stores datetimes as naive UTC.
    if dt.tzinfo is not None:
        from datetime import timezone
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.strftime('%Y-%m-%d %H:%M:%S')


def _find_or_upgrade_partner(customer_data, origin='odoo_manual', fallback_ref=None):
    """Find, upgrade, or create a res.partner.

    Always returns a partner (never None). For anonymous walk-ins with no
    identifying data, creates a stub named 'Walk-in #<fallback_ref>' with
    x_is_stub=True and x_partner_origin=origin, so the lane-watcher can
    still attach orders to a partner for kanban display. The nightly merge
    cron (Phase 6) reconciles stubs with full records.

    Match order (strongest first):
      1. x_dutchie_customer_id
      2. x_dutchie_loyalty_id
      3. phone last-10
      4. email (case-insensitive)

    Non-destructive upgrade: when a match is found and the incoming payload
    has fields the existing partner lacks, those fields are filled in.
    """
    customer_data = customer_data or {}
    Partner = request.env['res.partner'].sudo()

    phone = _normalize_phone(customer_data.get('phone', ''))
    email = (customer_data.get('email') or '').strip().lower()
    dutchie_customer_id = str(customer_data.get('dutchie_customer_id') or '').strip()
    dutchie_loyalty_id = str(customer_data.get('dutchie_loyalty_id') or '').strip()
    first_name = (customer_data.get('first_name') or '').strip()
    last_name = (customer_data.get('last_name') or '').strip()
    full_name = ' '.join(filter(None, [first_name, last_name])).strip()

    partner = None

    if dutchie_customer_id:
        partner = Partner.search(
            [('x_dutchie_customer_id', '=', dutchie_customer_id)], limit=1)
    if not partner and dutchie_loyalty_id:
        partner = Partner.search(
            [('x_dutchie_loyalty_id', '=', dutchie_loyalty_id)], limit=1)
    if not partner and phone:
        partner = Partner.search([('phone', 'ilike', phone[-10:])], limit=1)
    if not partner and email:
        partner = Partner.search([('email', '=ilike', email)], limit=1)

    if partner:
        # Non-destructive upgrade: fill in fields the existing partner lacks.
        updates = {}
        if dutchie_customer_id and not partner.x_dutchie_customer_id:
            updates['x_dutchie_customer_id'] = dutchie_customer_id
        if dutchie_loyalty_id and not partner.x_dutchie_loyalty_id:
            updates['x_dutchie_loyalty_id'] = dutchie_loyalty_id
        if phone and not partner.phone:
            updates['phone'] = phone
        if email and not partner.email:
            updates['email'] = email
        if full_name and (not partner.name or partner.name.startswith('Walk-in #')):
            updates['name'] = full_name
        if updates:
            partner.write(updates)
            partner._recompute_stub_flag()
            _logger.info('Upgraded partner %s with %s', partner.id, list(updates))
        return partner

    # No match — create. Anonymous walk-ins (no identifying data) still get
    # a stub so the kanban card has a partner to hang off.
    name = full_name or email or phone or (
        'Walk-in #%s' % fallback_ref if fallback_ref else 'Walk-in'
    )
    has_identity = bool(dutchie_customer_id or dutchie_loyalty_id or phone or email)
    is_stub = not (has_identity and full_name)

    values = {
        'name': name,
        'email': email or False,
        'phone': phone or False,
        'customer_rank': 1,
        'x_partner_origin': origin,
        'x_is_stub': is_stub,
        'x_first_seen_at': fields.Datetime.now(),
    }
    if dutchie_customer_id:
        values['x_dutchie_customer_id'] = dutchie_customer_id
    if dutchie_loyalty_id:
        values['x_dutchie_loyalty_id'] = dutchie_loyalty_id

    partner = Partner.create(values)
    _logger.info(
        'Created %s partner %s: %s (origin=%s)',
        'stub' if is_stub else 'full', partner.id, name, origin,
    )
    return partner


# Backwards-compat alias — remove once nothing else references it.
_find_or_create_partner = _find_or_upgrade_partner


def _upsert_order(order_data, Order, Line, default_origin='dutchie_walkin',
                  allow_create=True, backfill_mode=False):
    """Find-or-create a mint.pos.order from a Dutchie payload.

    Used by both bulk_sync (loop) and upsert_from_dutchie (single order).

    Match order: receipt_no → checkout_id → dutchie_shipment_id, all
    scoped by company_id. On match, state is updated and identifiers are
    backfilled without clobbering existing non-empty fields.

    Returns dict with:
      - created: True iff a new order row was created
      - updated: True iff an existing order row was updated
      - skipped: True iff no action was taken
      - order_id, order_name, partner_id: set on created/updated
      - reason: human-readable note, set on skipped
    """
    company_id = order_data.get('company_id')
    receipt_no = order_data.get('dutchie_receipt_no', '') or ''
    checkout_id = order_data.get('dutchie_checkout_id', '') or ''
    shipment_id = str(order_data.get('dutchie_shipment_id') or '')

    if not company_id:
        return {'created': False, 'updated': False, 'skipped': True,
                'reason': 'missing company_id'}

    existing = None
    if receipt_no:
        existing = Order.search([
            ('dutchie_receipt_no', '=', receipt_no),
            ('company_id', '=', company_id),
        ], limit=1)
    if not existing and checkout_id:
        existing = Order.search([
            ('dutchie_checkout_id', '=', checkout_id),
            ('company_id', '=', company_id),
        ], limit=1)
    if not existing and shipment_id:
        existing = Order.search([
            ('dutchie_shipment_id', '=', shipment_id),
            ('company_id', '=', company_id),
        ], limit=1)

    if existing:
        update_vals = {}
        new_state = order_data.get('state')
        if new_state and new_state != existing.state:
            update_vals['state'] = new_state
        if receipt_no and not existing.dutchie_receipt_no:
            update_vals['dutchie_receipt_no'] = receipt_no
        if order_data.get('dutchie_order_number') and not existing.dutchie_order_number:
            update_vals['dutchie_order_number'] = order_data['dutchie_order_number']
        if shipment_id and not existing.dutchie_shipment_id:
            update_vals['dutchie_shipment_id'] = shipment_id
        # Order-level totals: only set if currently empty/zero so manual
        # Odoo edits aren't clobbered, but a stub created by the lane-watcher
        # (with subtotal/total still 0) gets enriched on the next sync.
        for src in ('subtotal', 'discount_total', 'tax_total', 'total'):
            incoming = order_data.get(src)
            if incoming is not None and not existing[src]:
                update_vals[src] = incoming
        # Rich Dutchie fields: only fill what's currently empty so manual
        # Odoo edits aren't clobbered by a later sync.
        rich_vals = _build_rich_order_vals(order_data)
        for k, v in rich_vals.items():
            if not existing[k]:
                update_vals[k] = v
        if update_vals:
            existing.write(update_vals)

        # Populate line items if the existing row has none. The lane-watcher
        # creates stubs without items (the v2/guest/checked-in payload doesn't
        # carry cart contents); orderSync's bulk-sync run is the first place
        # that has them. Skip if the row already has lines so we don't dupe
        # or clobber manual edits.
        items = order_data.get('items') or []
        line_added = False
        if items and not existing.line_ids:
            company = request.env['res.company'].sudo().browse(company_id)
            if company.exists():
                LineForOrder = Line.with_company(company)
                for item in items:
                    line_vals = {
                        'order_id': existing.id,
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
                    }
                    line_vals.update(_build_rich_line_vals(item))
                    LineForOrder.create(line_vals)
                line_added = True

        if update_vals or line_added:
            return {'created': False, 'updated': True, 'skipped': False,
                    'order_id': existing.id, 'order_name': existing.name,
                    'partner_id': existing.partner_id.id if existing.partner_id else False}
        return {'created': False, 'updated': False, 'skipped': True,
                'reason': 'no changes',
                'order_id': existing.id, 'order_name': existing.name,
                'partner_id': existing.partner_id.id if existing.partner_id else False}

    if not allow_create:
        return {'created': False, 'updated': False, 'skipped': True,
                'reason': 'not found (allow_create=False)'}

    # Guard: don't resurrect terminal-state walk-ins that were never in Odoo.
    # This is specifically for the lane-watcher path — historical back-fills
    # (bulk_sync with backfill_mode=True) legitimately create completed rows.
    incoming_state = order_data.get('state') or 'completed'
    if (incoming_state in ('completed', 'cancelled')
            and default_origin == 'dutchie_walkin'
            and not backfill_mode):
        return {'created': False, 'updated': False, 'skipped': True,
                'reason': 'terminal state for unseen order — skipped create'}

    # Create. Always returns a partner (stub if anonymous walk-in).
    partner = _find_or_upgrade_partner(
        order_data.get('customer'),
        origin=default_origin,
        fallback_ref=shipment_id or receipt_no or checkout_id,
    )

    # Resolve order_type: explicit order_type wins; else map from Dutchie
    # order_source ("Walk In"→in_store, "Dutchie"/"Leafly"→pickup, …).
    raw_type = order_data.get('order_type')
    if raw_type:
        order_type = ORDER_TYPE_MAP.get(raw_type, 'in_store')
    elif order_data.get('order_source'):
        order_type = ORDER_SOURCE_TYPE_MAP.get(order_data['order_source'], 'in_store')
    else:
        order_type = 'in_store'

    totals = order_data.get('totals', {})
    order_vals = {
        'partner_id': partner.id,
        'company_id': company_id,
        'dutchie_checkout_id': checkout_id or False,
        'dutchie_receipt_no': receipt_no or False,
        'state': incoming_state,
        'order_type': order_type,
        'payment_method': order_data.get('payment_method', 'cash'),
        'subtotal': totals.get('subtotal', order_data.get('subtotal', 0)),
        'discount_total': totals.get('discounts', order_data.get('discount_total', 0)),
        'tax_total': totals.get('taxes', order_data.get('tax_total', 0)),
        'total': totals.get('total', order_data.get('total', 0)),
        'notes': order_data.get('notes', ''),
        'source': order_data.get('source', 'dutchie_sync'),
    }
    if shipment_id:
        order_vals['dutchie_shipment_id'] = shipment_id
    if order_data.get('placed_at'):
        normalized = _normalize_datetime(order_data['placed_at'])
        if normalized:
            order_vals['placed_at'] = normalized
    # Merge rich Dutchie fields (payment breakdown, customer dob,
    # order source, terminal, etc.).
    order_vals.update(_build_rich_order_vals(order_data))

    company = request.env['res.company'].sudo().browse(company_id)
    if not company.exists():
        raise ValueError(f'company_id {company_id} not found')
    order = Order.with_company(company).create(order_vals)

    # Pin the line env to the same company so any _check_company_auto on
    # line fields resolves against the store, not env's default company.
    LineForOrder = Line.with_company(company)
    for item in order_data.get('items', []):
        line_vals = {
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
        }
        line_vals.update(_build_rich_line_vals(item))
        LineForOrder.create(line_vals)

    # Auto-consume any pending /rewards redemption whose product appears as
    # a free line in this order. Depends on budtender zeroing the reward
    # item. Skipped in backfill mode — historical orders shouldn't
    # retroactively mark pending loyalty redemptions as used.
    if not backfill_mode and 'mint.discount' in request.env:
        try:
            consumed = request.env['mint.discount'].sudo().consume_pending_redemption(
                partner, order_items=order_data.get('items', []),
            )
            if consumed:
                _logger.info(
                    'Auto-consumed redemption %s for %s on POS sync (order %s)',
                    consumed.redemption_code, partner.name, order.name,
                )
        except Exception:
            _logger.exception(
                'Redemption consume failed for order %s', order.name,
            )

    return {'created': True, 'updated': False, 'skipped': False,
            'order_id': order.id, 'order_name': order.name,
            'partner_id': partner.id}


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


# Dutchie "orderSource" (txn-level) → order_type. Walk-in cashier sales
# become in-store; the rest (third-party menus, Dutchie's own online) are
# pickup-style. Delivery isn't surfaced through orderSource.
ORDER_SOURCE_TYPE_MAP = {
    'Walk In': 'in_store',
    'Dutchie': 'pickup',
    'Leafly': 'pickup',
    'Weedmaps': 'pickup',
    'MintDeals': 'pickup',
    'Kiosk': 'in_store',
}


def _build_rich_order_vals(order_data):
    """Map the rich Dutchie transaction fields from order_data onto
    mint.pos.order columns. Only includes keys present in order_data —
    safe to splat into both create and update writes.
    """
    vals = {}
    # Payment split + loyalty dollar values + tip/change
    for src, dst in (
        ('cash_paid', 'cash_paid'),
        ('debit_paid', 'debit_paid'),
        ('credit_paid', 'credit_paid'),
        ('electronic_paid', 'electronic_paid'),
        ('integrated_paid', 'integrated_paid'),
        ('manual_paid', 'manual_paid'),
        ('gift_paid', 'gift_paid'),
        ('mmap_paid', 'mmap_paid'),
        ('change_due', 'change_due'),
        ('tip_amount', 'tip_amount'),
        ('loyalty_earned', 'loyalty_earned'),
        ('loyalty_spent', 'loyalty_spent'),
    ):
        if src in order_data and order_data[src] is not None:
            vals[dst] = order_data[src]
    # Strings / flags
    if order_data.get('payment_method_label'):
        vals['payment_method_label'] = order_data['payment_method_label']
    if order_data.get('order_source'):
        vals['order_source'] = order_data['order_source']
    if order_data.get('was_preordered') is not None:
        vals['was_preordered'] = bool(order_data['was_preordered'])
    if order_data.get('is_medical') is not None:
        vals['is_medical'] = bool(order_data['is_medical'])
    # Customer enrichment
    for k in ('dutchie_customer_id', 'customer_type_id',
              'customer_patient_type', 'customer_mj_state_id'):
        if order_data.get(k):
            vals[k] = order_data[k]
    if order_data.get('customer_dob'):
        # Accept ISO "1991-09-12" or "1991-09-12T00:00:00" — keep date part.
        dob = str(order_data['customer_dob'])
        vals['customer_dob'] = dob[:10] if len(dob) >= 10 else dob
    # Terminal / employee
    for k in ('terminal_name', 'dutchie_employee_id'):
        if order_data.get(k):
            vals[k] = order_data[k]
    if order_data.get('checkin_at'):
        normalized = _normalize_datetime(order_data['checkin_at'])
        if normalized:
            vals['checkin_at'] = normalized
    return vals


def _build_rich_line_vals(item):
    """Map the rich Dutchie line fields onto mint.pos.order.line columns."""
    vals = {}
    for src, dst in (
        ('dutchie_package_id', 'dutchie_package_id'),
        ('dutchie_inventory_id', 'dutchie_inventory_id'),
        ('dutchie_transaction_item_id', 'dutchie_transaction_item_id'),
        ('unit_cost', 'unit_cost'),
        ('vendor', 'vendor'),
        ('unit_weight_value', 'unit_weight_value'),
        ('unit_weight_unit', 'unit_weight_unit'),
        ('return_reason', 'return_reason'),
    ):
        if src in item and item[src] not in (None, ''):
            vals[dst] = item[src]
    if item.get('is_returned') is not None:
        vals['is_returned'] = bool(item['is_returned'])
    if item.get('return_date'):
        normalized = _normalize_datetime(item['return_date'])
        if normalized:
            vals['return_date'] = normalized
    # Preserve raw Dutchie arrays as JSON text — compliance/margin rebuilds
    # rely on per-discount and per-tax-rate amounts, not just scalar rollups.
    if item.get('discounts'):
        vals['dutchie_discounts_json'] = json.dumps(item['discounts'])
    if item.get('taxes'):
        vals['dutchie_taxes_json'] = json.dumps(item['taxes'])
    return vals


class MintPosOrderAPI(http.Controller):

    # ── CORS preflight ────────────────────────────────────────────────

    @http.route([
        '/api/v1/pos/orders',
        '/api/v1/pos/orders/<int:order_id>/state',
        '/api/v1/pos/orders/bulk-sync',
        '/api/v1/pos/orders/upsert-from-dutchie',
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
        partner = _find_or_upgrade_partner(
            data.get('customer'), origin='web_checkout',
        )

        # Map order type
        raw_type = data.get('order_type', 'pickup')
        order_type = ORDER_TYPE_MAP.get(raw_type, 'pickup')

        # Map payment method (frontend sends 'in-store', model expects 'cash')
        raw_pm = data.get('payment_method', 'online')
        pm_map = {'in-store': 'cash', 'pay-at-store': 'cash'}
        payment_method = pm_map.get(raw_pm, raw_pm) if raw_pm else 'online'

        # Build order values. Totals fall back to top-level keys so payloads
        # from BullMQ webOrder (which sends subtotal/total at the root) match
        # payloads from orderSync (which nests them under "totals"). Without
        # this fallback, Pay-at-Store orders persist with $0 totals.
        totals = data.get('totals', {})
        order_vals = {
            'partner_id': partner.id if partner else False,
            'company_id': company.id,
            'dutchie_checkout_id': data.get('dutchie_checkout_id', ''),
            'dutchie_receipt_no': data.get('dutchie_receipt_no', ''),
            'state': data.get('state', 'placed'),
            'order_type': order_type,
            'payment_method': payment_method,
            'subtotal': totals.get('subtotal', data.get('subtotal', 0)),
            'discount_total': totals.get('discounts', data.get('discount_total', 0)),
            'tax_total': totals.get('taxes', data.get('tax_total', 0)),
            'total': totals.get('total', data.get('total', 0)),
            'notes': data.get('notes', ''),
            'loyalty_points_earned': data.get('loyalty_points_earned', 0),
            'loyalty_points_redeemed': data.get('loyalty_points_redeemed', 0),
        }

        # Without dutchie_shipment_id the Odoo→Dutchie cancel webhook
        # (server.js /api/webhook/lane-change) can't flag the swimlane entry,
        # leaving cancelled orders live in the POS UI.
        shipment_id = str(data.get('dutchie_shipment_id') or '')
        if shipment_id:
            order_vals['dutchie_shipment_id'] = shipment_id
        if data.get('dutchie_customer_id'):
            try:
                order_vals['dutchie_customer_id'] = int(data['dutchie_customer_id'])
            except (TypeError, ValueError):
                pass
        if data.get('dutchie_order_number'):
            order_vals['dutchie_order_number'] = data['dutchie_order_number']

        if data.get('placed_at'):
            normalized = _normalize_datetime(data['placed_at'])
            if normalized:
                order_vals['placed_at'] = normalized
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

        # Create line items — pin to order's company so _check_company_auto resolves
        # against the store, not env's default company (matches _upsert_order pattern)
        Line = request.env['mint.pos.order.line'].sudo().with_company(company)
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

        # Filter by dutchie_shipment_id (stable POS-assigned unique key — preferred for lane watcher lookups)
        if kw.get('dutchie_shipment_id'):
            domain.append(('dutchie_shipment_id', '=', kw['dutchie_shipment_id']))

        # Filter by partner_id (authenticated frontend lookups — JWT → partner_id).
        # Frontend sends this as a header so it never lands in nginx/Railway access logs;
        # also accept a query param for tooling/admin use.
        partner_id = (
            request.httprequest.headers.get('X-Partner-Id')
            or kw.get('partner_id')
        )
        if partner_id:
            domain.append(('partner_id', '=', int(partner_id)))

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

    # ── PUT /api/v1/pos/orders/<id>/dutchie-status — Relay callback ──
    # Called by the inventory-service dutchiePosRelay after checking in a
    # guest at Dutchie. Persists the Dutchie-assigned ShipmentId (stable key
    # the lane watcher uses) and receipt number back onto the mint.pos.order.

    @http.route('/api/v1/pos/orders/<int:order_id>/dutchie-status',
                type='http', auth='none', methods=['PUT', 'OPTIONS'],
                csrf=False, cors='*')
    def update_dutchie_status(self, order_id, **kw):
        if request.httprequest.method == 'OPTIONS':
            return _json({})
        if not _verify_api_key():
            return _error('Invalid API key', 401)

        try:
            data = json.loads(request.httprequest.data)
        except (json.JSONDecodeError, TypeError):
            return _error('Invalid JSON body')

        order = request.env['mint.pos.order'].sudo().browse(order_id)
        if not order.exists():
            return _error('Order not found', 404)

        vals = {}
        if data.get('success'):
            if data.get('dutchie_receipt_no'):
                vals['dutchie_receipt_no'] = data['dutchie_receipt_no']
            if data.get('dutchie_shipment_id'):
                vals['dutchie_shipment_id'] = str(data['dutchie_shipment_id'])
            if data.get('dutchie_order_number'):
                vals['dutchie_order_number'] = data['dutchie_order_number']
        else:
            err = data.get('error') or 'Unknown relay error'
            vals['notes'] = (order.notes or '') + '\nDutchie relay failed: ' + err

        if vals:
            order.write(vals)

        _logger.info(
            'Dutchie status for %s: success=%s shipment=%s receipt=%s',
            order.name, data.get('success'),
            vals.get('dutchie_shipment_id', '-'),
            vals.get('dutchie_receipt_no', '-'),
        )

        return _json({
            'success': True,
            'order_id': order.id,
            'order_ref': order.name,
            'dutchie_shipment_id': order.dutchie_shipment_id or None,
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

        reason = (data.get('reason') or '').strip()
        if new_state == 'cancelled' and reason:
            vals['notes'] = (order.notes or '') + '\nCancelled: ' + reason

        order.with_context(cancel_reason=reason).write(vals)

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
        # Backfill mode: disables business-logic side effects that assume a
        # live transaction (loyalty redemption consume, terminal-state guard
        # for walk-ins, and any future deal activation hooks). Historical
        # orders pushed by the parity probe or a manual catch-up run should
        # NOT retroactively mark pending redemptions as used, and SHOULD be
        # allowed to create terminal-state rows that were never seen live.
        backfill_mode = bool(data.get('backfill'))
        if backfill_mode:
            _logger.info('bulk-sync: backfill mode — loyalty/terminal guards disabled')
        if not orders_data:
            return _json({'created': 0, 'updated': 0, 'errors': 0})

        Order = request.env['mint.pos.order'].sudo()
        Line = request.env['mint.pos.order.line'].sudo()

        created = 0
        updated = 0
        errors = 0
        error_details = []

        for order_data in orders_data:
            try:
                result = _upsert_order(
                    order_data, Order, Line,
                    default_origin='dutchie_walkin',
                    allow_create=True,
                    backfill_mode=backfill_mode,
                )
                if result.get('created'):
                    created += 1
                elif result.get('updated'):
                    updated += 1
            except Exception as e:
                _logger.exception(
                    'Error syncing order receipt=%s checkout=%s',
                    order_data.get('dutchie_receipt_no'),
                    order_data.get('dutchie_checkout_id'),
                )
                errors += 1
                if len(error_details) < 10:
                    error_details.append({
                        'receipt': order_data.get('dutchie_receipt_no'),
                        'company_id': order_data.get('company_id'),
                        'error': type(e).__name__ + ': ' + str(e)[:300],
                    })

        _logger.info(
            'Bulk sync complete: %d created, %d updated, %d errors',
            created, updated, errors,
        )

        payload = {
            'created': created,
            'updated': updated,
            'errors': errors,
        }
        if error_details:
            payload['error_details'] = error_details
        return _json(payload)

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

    # ── POST /api/v1/pos/orders/upsert-from-dutchie ───────────────────
    #
    # Single-order upsert used by the lane watcher when it detects a lane
    # change for a Dutchie shipment not yet in Odoo. Creates a stub order
    # (with a stub partner if needed) so the kanban can display the card.

    @http.route('/api/v1/pos/orders/upsert-from-dutchie', type='http',
                auth='none', methods=['POST', 'OPTIONS'], csrf=False, cors='*')
    def upsert_from_dutchie(self, **kw):
        if not _verify_api_key():
            return _error('Invalid API key', 401)

        try:
            order_data = json.loads(request.httprequest.data)
        except (json.JSONDecodeError, TypeError):
            return _error('Invalid JSON body')

        if not isinstance(order_data, dict):
            return _error('Expected single order object')

        allow_create = bool(order_data.pop('allow_create', True))

        Order = request.env['mint.pos.order'].sudo()
        Line = request.env['mint.pos.order.line'].sudo()

        try:
            result = _upsert_order(
                order_data, Order, Line,
                default_origin='dutchie_walkin',
                allow_create=allow_create,
            )
        except Exception as e:
            _logger.exception(
                'upsert-from-dutchie failed for shipment=%s ref=%s',
                order_data.get('dutchie_shipment_id'),
                order_data.get('dutchie_receipt_no') or order_data.get('dutchie_checkout_id'),
            )
            return _error(type(e).__name__ + ': ' + str(e)[:300], 500)

        return _json(result)

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
                ('dutchie_shipment_id', '=', str(data['dutchie_shipment_id']))
            ], limit=1)
        if not order and data.get('dutchie_receipt_no'):
            order = Order.search([
                ('dutchie_receipt_no', '=', str(data['dutchie_receipt_no']))
            ], limit=1)

        if not order:
            return _json({'error': 'Order not found'}, 404)

        if order.state in ('completed', 'picked_up', 'delivery_completed'):
            return _json(
                {'error': 'Cannot cancel an order that is already %s' % order.state},
                400,
            )

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
