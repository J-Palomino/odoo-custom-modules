# -*- coding: utf-8 -*-
"""Phase 0 reference-data endpoints: health, markets, locations, profile.

Market scoping rule (applies to every domain route in later phases too):
non-admin responses are filtered to the mint.region slugs in the caller's
mint.dashboard.permission.market_ids.
"""
import logging

from odoo import http
from odoo.http import request

from .base import (
    allowed_market_slugs,
    error_response,
    json_response,
    require_dashboard_auth,
    verify_employee,
)

_logger = logging.getLogger(__name__)


class MintDashboardReference(http.Controller):

    @http.route('/api/v1/dashboard/health', type='http', auth='none',
                methods=['GET'], csrf=False, cors='*')
    def health(self):
        return json_response({
            'status': 'healthy',
            'service': 'mint-dashboard-api',
            'version': '0.1.0',
        })

    @http.route('/api/v1/dashboard/markets', type='http', auth='none',
                methods=['GET', 'OPTIONS'], csrf=False, cors='*')
    @require_dashboard_auth()
    def markets(self, **kw):
        slugs = allowed_market_slugs()
        domain = [] if slugs is None else [('slug', 'in', slugs)]
        regions = request.env['mint.region'].sudo().search(domain, order='name')
        return json_response({'markets': [{
            'id': r.id,
            'name': r.name,
            'slug': r.slug,
            'code': r.code or '',
            'store_count': r.store_count,
        } for r in regions]})

    @http.route('/api/v1/dashboard/locations', type='http', auth='none',
                methods=['GET', 'OPTIONS'], csrf=False, cors='*')
    @require_dashboard_auth()
    def locations(self, **kw):
        slugs = allowed_market_slugs()
        domain = ['|', ('dutchie_store_id', '!=', False), ('store_code', '!=', False)]
        if slugs is not None:
            domain += [('region_id.slug', 'in', slugs)]
        stores = request.env['res.company'].sudo().search(domain, order='name')
        return json_response({'locations': [{
            'id': c.id,
            'name': c.name,
            'store_code': c.store_code or '',
            'market_slug': c.region_id.slug if c.region_id else None,
            'dutchie_store_id': c.dutchie_store_id or None,
        } for c in stores]})

    @http.route('/api/v1/dashboard/profile', type='http', auth='none',
                methods=['GET', 'OPTIONS'], csrf=False, cors='*')
    def profile(self, **kw):
        if request.httprequest.method == 'OPTIONS':
            return json_response({})
        user = verify_employee()
        if not user:
            return error_response('Unauthorized', 401)
        from .auth import _profile_payload
        return json_response({'user': _profile_payload(user)})
