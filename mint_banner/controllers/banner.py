# -*- coding: utf-8 -*-
"""
MintDeals Banner API — /api/v1/banners

Endpoint for retrieving active category banners.
"""
import base64
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
            'Access-Control-Allow-Methods': 'GET, OPTIONS',
            'Access-Control-Allow-Headers': 'Content-Type, Authorization',
        }
    )


def error_response(message, status=400):
    """Helper to create error responses."""
    return json_response({'error': message, 'status': status}, status=status)


class MintBannerAPI(http.Controller):
    """REST API Controller for banner retrieval."""

    @http.route('/api/v1/banners', type='http', auth='none',
                methods=['GET', 'OPTIONS'], csrf=False, cors='*')
    def get_banners(self):
        """Return active banners filtered by slot, category, and company."""
        slot = request.params.get('slot')
        category = request.params.get('category')
        company_id = request.params.get('company_id')

        domain = [('active', '=', True)]

        if slot:
            domain.append(('slot', '=', slot))
        if category:
            domain.append(('category', '=', category))
        if company_id:
            domain.append(('company_id', '=', int(company_id)))

        today = fields.Date.today()
        domain.append('|')
        domain.append(('date_start', '=', False))
        domain.append(('date_start', '<=', today))
        domain.append('|')
        domain.append(('date_end', '=', False))
        domain.append(('date_end', '>=', today))

        Banner = request.env['mint.banner'].sudo()
        banners = Banner.search(domain, order='sequence')

        result = []
        for b in banners:
            # Prefer external URL over binary image
            if b.image_url:
                image_data = b.image_url
            elif b.image:
                image_data = 'data:image/png;base64,' + base64.b64encode(
                    base64.b64decode(b.image)
                ).decode('utf-8') if isinstance(b.image, str) else (
                    'data:image/png;base64,' + base64.b64encode(b.image).decode('utf-8')
                )
            else:
                image_data = None

            result.append({
                'id': b.id,
                'name': b.name,
                'slot': b.slot,
                'category': b.category or '',
                'image': image_data,
                'image_url': b.image_url or '',
                'title': b.title or '',
                'subtitle': b.subtitle or '',
                'link_url': b.link_url or '',
                'link_label': b.link_label or '',
                'background_color': b.background_color or '',
                'sequence': b.sequence,
            })

        return json_response(result)
