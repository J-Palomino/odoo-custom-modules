# -*- coding: utf-8 -*-
"""
Push Notification API endpoints for the MintDeals PWA.

These routes match what the frontend (src/pwa/push.ts) expects:
  GET  /api/v1/push/vapid-key   → {publicKey}
  POST /api/v1/push/subscribe   → {ok}
  POST /api/v1/push/unsubscribe → {ok}
"""
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
            'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
            'Access-Control-Allow-Headers': 'Content-Type, Authorization',
        },
    )


def _error(message, status=400):
    return _json({'error': message, 'status': status}, status=status)


def _cors_preflight():
    """Return a 204 response for CORS preflight requests."""
    return Response(
        '',
        status=204,
        headers={
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
            'Access-Control-Allow-Headers': 'Content-Type, Authorization',
            'Access-Control-Max-Age': '86400',
        },
    )


def _partner_id_from_jwt():
    """Partner id proven by the caller's Authorization: Bearer JWT, else False.

    The ONLY accepted identity for binding a push subscription to a customer.
    Body-supplied partner_id/email are never trusted on this auth='none'
    route: an unauthenticated caller could otherwise bind their device to an
    arbitrary partner and receive that partner's notifications (welcome
    coupon codes are bearer instruments at the register).
    """
    auth_header = request.httprequest.headers.get('Authorization', '')
    if not auth_header.startswith('Bearer '):
        return False
    Users = request.env['res.users'].sudo()
    if not hasattr(Users, '_verify_jwt'):
        return False
    try:
        payload = Users._verify_jwt(auth_header[7:])
        if not payload:
            return False
        user = Users.browse(payload.get('user_id'))
        if not user.exists() or not user.partner_id:
            return False
        return user.partner_id.id
    except Exception as e:
        _logger.warning('push subscribe: JWT verify failed: %s', e)
        return False


class PushAPI(http.Controller):

    # ==================== CORS PREFLIGHT ====================

    @http.route(
        ['/api/v1/push/vapid-key', '/api/v1/push/subscribe', '/api/v1/push/unsubscribe'],
        type='http', auth='none', methods=['OPTIONS'], csrf=False, cors='*',
    )
    def push_options(self, **kwargs):
        return _cors_preflight()

    # ==================== VAPID KEY ====================

    @http.route('/api/v1/push/vapid-key', type='http', auth='none',
                methods=['GET'], csrf=False, cors='*')
    def get_vapid_key(self, **kwargs):
        """Return the public VAPID key for push subscription."""
        try:
            ICP = request.env['ir.config_parameter'].sudo()
            public_key = ICP.get_param('mint.vapid_public_key')

            if not public_key:
                return _error('VAPID keys not configured. Run setup-vapid-keys.mjs first.', 503)

            return _json({'publicKey': public_key})
        except Exception as e:
            _logger.error('Error fetching VAPID key: %s', e)
            return _error(str(e), 500)

    # ==================== SUBSCRIBE ====================

    @http.route('/api/v1/push/subscribe', type='http', auth='none',
                methods=['POST'], csrf=False, cors='*')
    def subscribe(self, **kwargs):
        """Register a new push subscription from the browser."""
        try:
            data = json.loads(request.httprequest.data or '{}')
            endpoint = data.get('endpoint')
            keys = data.get('keys', {})
            p256dh = keys.get('p256dh')
            auth = keys.get('auth')

            if not endpoint or not p256dh or not auth:
                return _error('Missing required fields: endpoint, keys.p256dh, keys.auth')

            Subscription = request.env['mint.push.subscription'].sudo()

            # Resolve site by code (optional)
            site_id = False
            site_code = data.get('site_code')
            if site_code:
                site = request.env['mint.push.site'].sudo().search(
                    [('code', '=', site_code)], limit=1)
                if site:
                    site_id = site.id

            # Resolve store by slug (optional)
            store_id = False
            store_slug = data.get('store_slug')
            if store_slug:
                store = request.env['res.company'].sudo().search(
                    [('x_slug', '=', store_slug)], limit=1)
                if store:
                    store_id = store.id

            region = data.get('region') or ''

            # GPS coordinates (optional, from cached location)
            latitude = data.get('latitude')
            longitude = data.get('longitude')

            # Partner binding comes from the verified JWT only (see helper).
            partner_id = _partner_id_from_jwt()

            # Upsert: reactivate if it already exists, or create new
            existing = Subscription.search([('endpoint', '=', endpoint)], limit=1)
            if existing:
                vals = {
                    'key_p256dh': p256dh,
                    'key_auth': auth,
                    'is_active': True,
                    'user_agent': request.httprequest.headers.get('User-Agent', ''),
                }
                if partner_id:
                    vals['partner_id'] = partner_id
                if site_id:
                    vals['site_id'] = site_id
                if store_id:
                    vals['store_id'] = store_id
                if region:
                    vals['region'] = region
                if latitude is not None and longitude is not None:
                    vals['latitude'] = float(latitude)
                    vals['longitude'] = float(longitude)
                    vals['geo_updated_at'] = fields.Datetime.now()
                existing.write(vals)
            else:
                create_vals = {
                    'endpoint': endpoint,
                    'key_p256dh': p256dh,
                    'key_auth': auth,
                    'is_active': True,
                    'site_id': site_id,
                    'store_id': store_id,
                    'region': region,
                    'user_agent': request.httprequest.headers.get('User-Agent', ''),
                }
                if partner_id:
                    create_vals['partner_id'] = partner_id
                if latitude is not None and longitude is not None:
                    create_vals['latitude'] = float(latitude)
                    create_vals['longitude'] = float(longitude)
                    create_vals['geo_updated_at'] = fields.Datetime.now()
                Subscription.create(create_vals)

            return _json({'ok': True})
        except Exception as e:
            _logger.error('Error subscribing to push: %s', e)
            return _error(str(e), 500)

    # ==================== UNSUBSCRIBE ====================

    @http.route('/api/v1/push/unsubscribe', type='http', auth='none',
                methods=['POST'], csrf=False, cors='*')
    def unsubscribe(self, **kwargs):
        """Deactivate a push subscription."""
        try:
            data = json.loads(request.httprequest.data or '{}')
            endpoint = data.get('endpoint')

            if not endpoint:
                return _error('Missing required field: endpoint')

            Subscription = request.env['mint.push.subscription'].sudo()
            existing = Subscription.search([('endpoint', '=', endpoint)], limit=1)
            if existing:
                existing.write({'is_active': False})

            return _json({'ok': True})
        except Exception as e:
            _logger.error('Error unsubscribing from push: %s', e)
            return _error(str(e), 500)
