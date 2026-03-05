# -*- coding: utf-8 -*-
"""
Embed API endpoints:
  GET  /api/v1/embed/pages/:slug   — fetch page content
  POST /api/v1/embed/contact       — submit contact form
  POST /api/v1/embed/newsletter    — submit newsletter signup
"""
import json
import logging
import re

from odoo import http, fields as odoo_fields
from odoo.http import request, Response

_logger = logging.getLogger(__name__)

EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


def _json(data, status=200):
    return Response(
        json.dumps(data, default=str),
        status=status,
        content_type='application/json',
        headers={
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
            'Access-Control-Allow-Headers': 'Content-Type',
        },
    )


def _error(message, status=400):
    return _json({'error': message}, status=status)


def _cors_preflight():
    return Response('', status=204, headers={
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
        'Access-Control-Allow-Headers': 'Content-Type',
        'Access-Control-Max-Age': '86400',
    })


class MintEmbedAPI(http.Controller):

    # ==================== CORS PREFLIGHT ====================

    @http.route(
        ['/api/v1/embed/contact', '/api/v1/embed/newsletter',
         '/api/v1/embed/pages/<string:slug>'],
        type='http', auth='none', methods=['OPTIONS'], csrf=False, cors='*',
    )
    def embed_options(self, **kwargs):
        return _cors_preflight()

    # ==================== PAGES ====================

    @http.route('/api/v1/embed/pages/<string:slug>', type='http',
                auth='none', methods=['GET'], csrf=False, cors='*')
    def get_page(self, slug):
        try:
            page = request.env['mint.embed.page'].sudo().search([
                ('slug', '=', slug),
                ('active', '=', True),
            ], limit=1)

            if not page:
                return _error('Page not found', 404)

            base_url = request.env['ir.config_parameter'].sudo().get_param(
                'web.base.url', '')

            hero = None
            if page.hero_image_url:
                hero = page.hero_image_url
            elif page.hero_image:
                hero = f'{base_url}/web/image/mint.embed.page/{page.id}/hero_image'

            return _json({
                'slug': page.slug,
                'title': page.name,
                'summary': page.summary or '',
                'content': page.content or '',
                'hero_image': hero,
                'cta_label': page.cta_label or '',
                'cta_url': page.cta_url or '',
            })
        except Exception as e:
            _logger.error('Error fetching embed page %s: %s', slug, e)
            return _error('Internal server error', 500)

    # ==================== CONTACT FORM ====================

    @http.route('/api/v1/embed/contact', type='http', auth='none',
                methods=['POST'], csrf=False, cors='*')
    def submit_contact(self, **kwargs):
        try:
            data = json.loads(request.httprequest.data or '{}')
            email = (data.get('email') or '').strip().lower()
            name = (data.get('name') or '').strip()
            message = (data.get('message') or '').strip()

            if not email or not EMAIL_RE.match(email):
                return _error('A valid email is required.')
            if not name:
                return _error('Name is required.')
            if not message:
                return _error('Message is required.')
            if len(message) > 10000:
                return _error('Message is too long.')

            Submission = request.env['mint.embed.submission'].sudo()
            Submission.create({
                'form_type': 'contact',
                'name': name[:200],
                'email': email[:254],
                'phone': (data.get('phone') or '').strip()[:30],
                'message': message,
                'source_url': (data.get('source_url') or '').strip()[:500],
                'embed_key': (data.get('embed_key') or '').strip()[:20],
                'ip_address': request.httprequest.remote_addr,
            })

            return _json({'ok': True, 'message': 'Thank you! We will be in touch.'})
        except Exception as e:
            _logger.error('Error submitting contact form: %s', e)
            return _error('Internal server error', 500)

    # ==================== NEWSLETTER ====================

    @http.route('/api/v1/embed/newsletter', type='http', auth='none',
                methods=['POST'], csrf=False, cors='*')
    def submit_newsletter(self, **kwargs):
        try:
            data = json.loads(request.httprequest.data or '{}')
            email = (data.get('email') or '').strip().lower()

            if not email or not EMAIL_RE.match(email):
                return _error('A valid email is required.')

            Submission = request.env['mint.embed.submission'].sudo()

            # Case-insensitive dedup
            existing = Submission.search([
                ('form_type', '=', 'newsletter'),
                ('email', '=', email),
            ], limit=1)
            if existing:
                return _json({'ok': True, 'message': 'You are already subscribed!'})

            Submission.create({
                'form_type': 'newsletter',
                'name': (data.get('name') or '').strip()[:200],
                'email': email[:254],
                'source_url': (data.get('source_url') or '').strip()[:500],
                'embed_key': (data.get('embed_key') or '').strip()[:20],
                'ip_address': request.httprequest.remote_addr,
            })

            return _json({'ok': True, 'message': 'Welcome! You are now subscribed.'})
        except Exception as e:
            _logger.error('Error submitting newsletter: %s', e)
            return _error('Internal server error', 500)
