# -*- coding: utf-8 -*-
"""
MintDeals Push Notification API — /api/v1/push/

Endpoints for managing browser push subscriptions and sending notifications.
"""
import json
import logging

from odoo import http, fields
from odoo.http import request, Response

_logger = logging.getLogger(__name__)


def json_response(data, status=200):
    """Helper to create JSON responses with proper headers."""
    return Response(
        json.dumps(data, default=str),
        status=status,
        content_type='application/json',
        headers={
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
            'Access-Control-Allow-Headers': 'Content-Type, Authorization',
        }
    )


def error_response(message, status=400):
    """Helper to create error responses."""
    return json_response({'error': message, 'status': status}, status=status)


class MintPushAPI(http.Controller):
    """REST API Controller for push notification management."""

    # ==================== SITES ====================

    @http.route('/api/v1/push/sites', type='http', auth='none',
                methods=['GET', 'OPTIONS'], csrf=False, cors='*')
    def list_sites(self):
        """Return all active push notification sites."""
        sites = request.env['mint.push.site'].sudo().search([('active', '=', True)])
        return json_response({
            'sites': [{
                'code': s.code,
                'name': s.name,
                'url': s.url or '',
            } for s in sites]
        })

    # ==================== VAPID KEY ====================

    @http.route('/api/v1/push/vapid-key', type='http', auth='none',
                methods=['GET', 'OPTIONS'], csrf=False, cors='*')
    def get_vapid_key(self):
        """Return the public VAPID key for push subscription.

        Auto-generates VAPID keys on first request if they don't exist.
        """
        Sub = request.env['mint.push.subscription'].sudo()
        public_key = Sub._ensure_vapid_keys()

        if not public_key:
            return error_response('VAPID key generation failed', 500)

        return json_response({'publicKey': public_key})

    # ==================== SUBSCRIBE ====================

    @http.route('/api/v1/push/subscribe', type='http', auth='none',
                methods=['POST', 'OPTIONS'], csrf=False, cors='*')
    def subscribe(self):
        """Store a browser push subscription."""
        try:
            data = json.loads(request.httprequest.data)
        except (json.JSONDecodeError, TypeError):
            return error_response('Invalid JSON body')

        endpoint = data.get('endpoint')
        keys = data.get('keys', {})
        p256dh = keys.get('p256dh') or ''
        auth = keys.get('auth') or ''

        if not endpoint:
            return error_response('Missing required field: endpoint')

        # Web Push requires p256dh + auth; native apps send device tokens instead
        is_native = data.get('platform') in ('ios', 'android')
        if not is_native and (not p256dh or not auth):
            return error_response('Missing required fields: keys.p256dh, keys.auth')

        Sub = request.env['mint.push.subscription'].sudo()

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

        # Platform detection (web, ios, android)
        platform = data.get('platform', 'web')
        if platform not in ('web', 'ios', 'android'):
            platform = 'web'
        device_token = data.get('device_token') or ''

        # GPS coordinates (optional, from cached location)
        latitude = data.get('latitude')
        longitude = data.get('longitude')

        # Resolve partner by partner_id or email (links push to user account)
        partner_id = False
        raw_partner_id = data.get('partner_id')
        email = data.get('email')
        if raw_partner_id:
            partner = request.env['res.partner'].sudo().browse(int(raw_partner_id))
            if partner.exists():
                partner_id = partner.id
        elif email:
            partner = request.env['res.partner'].sudo().search(
                [('email', '=ilike', email)], limit=1)
            if partner:
                partner_id = partner.id

        # Upsert: update existing or create new
        existing = Sub.search([('endpoint', '=', endpoint)], limit=1)
        if existing:
            vals = {
                'key_p256dh': p256dh,
                'key_auth': auth,
                'fail_count': 0,
                'platform': platform,
            }
            if device_token:
                vals['device_token'] = device_token
            if site_id:
                vals['site_id'] = site_id
            if store_id:
                vals['store_id'] = store_id
            if region:
                vals['region'] = region
            if partner_id:
                vals['partner_id'] = partner_id
            if latitude is not None and longitude is not None:
                vals['latitude'] = float(latitude)
                vals['longitude'] = float(longitude)
                vals['geo_updated_at'] = fields.Datetime.now()
            existing.write(vals)
            _logger.info("Updated push subscription: %s... platform=%s partner=%s",
                         endpoint[:60], platform, partner_id or 'anon')
            return json_response({'status': 'updated', 'id': existing.id})

        create_vals = {
            'endpoint': endpoint,
            'key_p256dh': p256dh,
            'key_auth': auth,
            'site_id': site_id,
            'store_id': store_id,
            'region': region,
            'platform': platform,
        }
        if device_token:
            create_vals['device_token'] = device_token
        if partner_id:
            create_vals['partner_id'] = partner_id
        if latitude is not None and longitude is not None:
            create_vals['latitude'] = float(latitude)
            create_vals['longitude'] = float(longitude)
            create_vals['geo_updated_at'] = fields.Datetime.now()
        sub = Sub.create(create_vals)
        _logger.info("New push subscription: %s... platform=%s partner=%s",
                     endpoint[:60], platform, partner_id or 'anon')
        return json_response({'status': 'subscribed', 'id': sub.id})

    # ==================== UNSUBSCRIBE ====================

    @http.route('/api/v1/push/unsubscribe', type='http', auth='none',
                methods=['POST', 'OPTIONS'], csrf=False, cors='*')
    def unsubscribe(self):
        """Remove a browser push subscription."""
        try:
            data = json.loads(request.httprequest.data)
        except (json.JSONDecodeError, TypeError):
            return error_response('Invalid JSON body')

        endpoint = data.get('endpoint')
        if not endpoint:
            return error_response('Missing required field: endpoint')

        Sub = request.env['mint.push.subscription'].sudo()
        existing = Sub.search([('endpoint', '=', endpoint)], limit=1)
        if existing:
            existing.unlink()
            _logger.info("Removed push subscription: %s...", endpoint[:60])
            return json_response({'status': 'unsubscribed'})

        return json_response({'status': 'not_found'}, 404)

    # ==================== LOCATION UPDATE ====================

    @http.route('/api/v1/push/location', type='http', auth='none',
                methods=['POST', 'OPTIONS'], csrf=False, cors='*')
    def update_location(self):
        """Lightweight GPS location update for an existing subscriber."""
        try:
            data = json.loads(request.httprequest.data)
        except (json.JSONDecodeError, TypeError):
            return error_response('Invalid JSON body')

        endpoint = data.get('endpoint')
        latitude = data.get('latitude')
        longitude = data.get('longitude')

        if not endpoint:
            return error_response('Missing required field: endpoint')
        if latitude is None or longitude is None:
            return error_response('Missing required fields: latitude, longitude')

        Sub = request.env['mint.push.subscription'].sudo()
        existing = Sub.search([('endpoint', '=', endpoint)], limit=1)
        if existing:
            existing.write({
                'latitude': float(latitude),
                'longitude': float(longitude),
                'geo_updated_at': fields.Datetime.now(),
            })
            return json_response({'ok': True})

        return json_response({'ok': True, 'status': 'not_found'})

    # ==================== SEND (admin) ====================

    @http.route('/api/v1/push/send', type='http', auth='user',
                methods=['POST', 'OPTIONS'], csrf=False, cors='*')
    def send_notification(self):
        """Send a push notification to all subscribers. Requires authenticated user."""
        try:
            data = json.loads(request.httprequest.data)
        except (json.JSONDecodeError, TypeError):
            return error_response('Invalid JSON body')

        title = data.get('title')
        body = data.get('body')
        url = data.get('url')
        icon = data.get('icon')
        image = data.get('image')       # Large preview image URL
        actions = data.get('actions')   # [{action, title, url}] — up to 2 buttons

        if not title or not body:
            return error_response('Missing required fields: title, body')

        # Optional site filter
        site_id = False
        site_code = data.get('site_code')
        if site_code:
            site = request.env['mint.push.site'].sudo().search(
                [('code', '=', site_code)], limit=1)
            if site:
                site_id = site.id

        Sub = request.env['mint.push.subscription'].sudo()
        sent = Sub.send_to_all(title, body, url=url, icon=icon,
                               image=image, actions=actions, site_id=site_id)

        return json_response({'status': 'sent', 'delivered': sent})
