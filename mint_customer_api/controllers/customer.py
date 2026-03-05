# -*- coding: utf-8 -*-
"""
Customer profile endpoints for MintDeals frontend.

All endpoints require JWT authentication via Authorization header.
"""
import json
import logging

from odoo import http
from odoo.http import request, Response

from .auth import json_response, error_response, _verify_and_get_user

_logger = logging.getLogger(__name__)


class MintCustomerProfile(http.Controller):
    """Customer profile controller."""

    @http.route('/api/v1/customer/profile', type='http', auth='none',
                methods=['GET', 'OPTIONS'], csrf=False, cors='*')
    def get_profile(self, **kw):
        """Read customer profile from res.partner."""
        if request.httprequest.method == 'OPTIONS':
            return json_response({})

        user = _verify_and_get_user()
        if not user:
            return error_response('Authentication required', 401)

        partner = user.partner_id.sudo()
        return json_response({
            'profile': {
                'id': partner.id,
                'name': partner.name,
                'email': partner.email,
                'phone': partner.phone or '',
                'mobile': partner.mobile or '',
                'street': partner.street or '',
                'city': partner.city or '',
                'state': partner.state_id.name if partner.state_id else '',
                'zip': partner.zip or '',
                'preferred_store_id': getattr(partner, 'x_preferred_store_id', False) and partner.x_preferred_store_id.id or None,
                'preferred_store_name': getattr(partner, 'x_preferred_store_id', False) and partner.x_preferred_store_id.name or None,
                'home_store_id': getattr(partner, 'x_home_store_id', False) and partner.x_home_store_id.id or None,
                'home_store_name': getattr(partner, 'x_home_store_id', False) and partner.x_home_store_id.name or None,
                'total_spend': getattr(partner, 'x_dutchie_total_spend', 0) or 0,
                'visit_count': getattr(partner, 'x_dutchie_visit_count', 0) or 0,
            },
        })

    @http.route('/api/v1/customer/profile', type='http', auth='none',
                methods=['PUT', 'OPTIONS'], csrf=False, cors='*')
    def update_profile(self, **kw):
        """Update customer profile fields."""
        if request.httprequest.method == 'OPTIONS':
            return json_response({})

        user = _verify_and_get_user()
        if not user:
            return error_response('Authentication required', 401)

        try:
            data = json.loads(request.httprequest.data)
        except (json.JSONDecodeError, TypeError):
            return error_response('Invalid JSON body')

        partner = user.partner_id.sudo()
        vals = {}

        # Only update allowed fields
        if 'name' in data:
            vals['name'] = data['name'].strip()
        if 'phone' in data:
            vals['phone'] = data['phone'].strip()
        if 'mobile' in data:
            vals['mobile'] = data['mobile'].strip()
        if 'preferred_store_id' in data:
            store_id = data['preferred_store_id']
            if store_id:
                store = request.env['res.company'].sudo().browse(int(store_id))
                if store.exists():
                    vals['x_preferred_store_id'] = store.id
            else:
                vals['x_preferred_store_id'] = False

        if vals:
            partner.write(vals)

        return json_response({
            'message': 'Profile updated',
            'profile': {
                'id': partner.id,
                'name': partner.name,
                'email': partner.email,
                'phone': partner.phone or '',
                'mobile': partner.mobile or '',
            },
        })
