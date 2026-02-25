# -*- coding: utf-8 -*-
"""
MintDeals Push Notification API — /api/v1/push/

Endpoints for managing browser push subscriptions and sending notifications.
"""
import json
import logging

from odoo import http
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

    # ==================== VAPID KEY ====================

    @http.route('/api/v1/push/vapid-key', type='http', auth='none',
                methods=['GET', 'OPTIONS'], csrf=False, cors='*')
    def get_vapid_key(self):
        """Return the public VAPID key for push subscription."""
        ICP = request.env['ir.config_parameter'].sudo()
        public_key = ICP.get_param('mint.vapid_public_key', '')

        if not public_key:
            return error_response('VAPID key not configured', 500)

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
        p256dh = keys.get('p256dh')
        auth = keys.get('auth')

        if not endpoint or not p256dh or not auth:
            return error_response('Missing required fields: endpoint, keys.p256dh, keys.auth')

        Sub = request.env['mint.push.subscription'].sudo()

        # Upsert: update existing or create new
        existing = Sub.search([('endpoint', '=', endpoint)], limit=1)
        if existing:
            existing.write({
                'key_p256dh': p256dh,
                'key_auth': auth,
                'fail_count': 0,
            })
            _logger.info("Updated push subscription: %s...", endpoint[:60])
            return json_response({'status': 'updated', 'id': existing.id})

        sub = Sub.create({
            'endpoint': endpoint,
            'key_p256dh': p256dh,
            'key_auth': auth,
        })
        _logger.info("New push subscription: %s...", endpoint[:60])
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

        Sub = request.env['mint.push.subscription'].sudo()
        sent = Sub.send_to_all(title, body, url=url, icon=icon,
                               image=image, actions=actions)

        return json_response({'status': 'sent', 'delivered': sent})
