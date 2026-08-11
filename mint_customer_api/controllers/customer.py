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


def _serialize_redemption(rec):
    """Shape a mint.discount redemption for JSON output."""
    product = rec.redemption_product_id
    reward = rec.redemption_reward_id
    label = (product.name if product
             else (reward.description or reward.display_name) if reward
             else rec.name)
    return {
        'id': rec.id,
        'code': rec.redemption_code,
        'product_id': product.id if product else None,
        'product_name': product.name if product else None,
        'reward_id': reward.id if reward else None,
        'reward_name': label,
        'points_cost': rec.redemption_points_cost,
        'status': rec.redemption_status,
        'created_at': rec.create_date.isoformat() if rec.create_date else None,
        'expires_at': rec.expires_at.isoformat() if rec.expires_at else None,
    }


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
                'street': partner.street or '',
                'city': partner.city or '',
                'state': partner.state_id.name if partner.state_id else '',
                'zip': partner.zip or '',
                'home_store_id': getattr(partner, 'x_home_store_id', False) and partner.x_home_store_id.id or None,
                'home_store_name': getattr(partner, 'x_home_store_id', False) and partner.x_home_store_id.name or None,
                'total_spend': getattr(partner, 'x_dutchie_total_spend', 0) or 0,
                'visit_count': getattr(partner, 'x_dutchie_visit_count', 0) or 0,
            },
        })

    @http.route('/api/v1/customer/loyalty', type='http', auth='none',
                methods=['GET', 'OPTIONS'], csrf=False, cors='*')
    def get_loyalty(self, **kw):
        """Get customer loyalty points and available rewards."""
        if request.httprequest.method == 'OPTIONS':
            return json_response({})

        user = _verify_and_get_user()
        if not user:
            return error_response('Authentication required', 401)

        if not user.partner_id:
            return error_response('No customer profile linked to this account', 400)

        partner = user.partner_id.sudo()

        # Find loyalty program
        program = request.env['loyalty.program'].sudo().search(
            [('program_type', '=', 'loyalty')], limit=1
        )
        if not program:
            return json_response({'loyalty': {'points': 0, 'program_name': 'Mint Rewards', 'point_name': 'Points', 'available_rewards': []}})

        card = request.env['loyalty.card'].sudo().search([
            ('partner_id', '=', partner.id),
            ('program_id', '=', program.id),
        ], limit=1)

        points = card.points if card else 0

        # Get available rewards
        rewards = []
        for reward in program.reward_ids:
            rewards.append({
                'id': reward.id,
                'name': reward.display_name,
                'type': reward.reward_type,
                'required_points': reward.required_points,
                'discount': reward.discount if reward.reward_type == 'discount' else None,
                'discount_max': reward.discount_max_amount,
                'eligible': points >= reward.required_points,
            })

        return json_response({
            'loyalty': {
                'program_name': program.name,
                'points': points,
                'point_name': program.portal_point_name or 'Points',
                'card_id': card.id if card else None,
                'total_spend': getattr(partner, 'x_dutchie_total_spend', 0) or 0,
                'visit_count': getattr(partner, 'x_dutchie_visit_count', 0) or 0,
                'available_rewards': rewards,
            },
        })

    @http.route('/api/v1/customer/loyalty/redeemables', type='http', auth='none',
                methods=['GET', 'OPTIONS'], csrf=False, cors='*')
    def list_redeemables(self, **kw):
        """List products flagged as points-redeemable.

        Optional query param `store_id` restricts to products available at
        that store (future use — frontend can filter by availability).
        """
        if request.httprequest.method == 'OPTIONS':
            return json_response({})

        products = request.env['product.template'].sudo().search([
            ('x_is_loyalty_redeemable', '=', True),
            ('x_loyalty_points_cost', '>', 0),
            ('active', '=', True),
        ], order='x_loyalty_points_cost asc, name asc')

        return json_response({
            'products': [{
                'id': p.id,
                'name': p.name,
                'brand': p.brand_id.name if p.brand_id else None,
                'category': p.master_category or (p.categ_id.name if p.categ_id else None),
                'strain_type': p.strain_type or None,
                'image_url': ('/web/image/product.template/%d/image_256' % p.id)
                             if p.image_256 else None,
                'points_cost': p.x_loyalty_points_cost,
                'list_price': p.list_price,
                'dutchie_product_id': p.dutchie_product_id or None,
                'sku': p.default_code or None,
            } for p in products],
        })

    @http.route('/api/v1/customer/loyalty/redeem', type='http', auth='none',
                methods=['POST', 'OPTIONS'], csrf=False, cors='*')
    def redeem_loyalty(self, **kw):
        """Redeem a loyalty product for the current user.

        Body: { "product_id": 42 }   — a product.template.id flagged redeemable

        Atomically deducts points from the user's loyalty.card and creates
        a mint.discount record with discount_type='loyalty_redemption' and
        a unique redemption code. Response includes the product so the
        client can add it to the cart at 100% off.
        """
        if request.httprequest.method == 'OPTIONS':
            return json_response({})

        user = _verify_and_get_user()
        if not user:
            return error_response('Authentication required', 401)
        if not user.partner_id:
            return error_response('No customer profile linked to this account', 400)

        try:
            data = json.loads(request.httprequest.data)
        except (json.JSONDecodeError, TypeError):
            return error_response('Invalid JSON body')

        product_id = data.get('product_id')
        if not product_id:
            return error_response('product_id is required')

        partner = user.partner_id.sudo()
        product = request.env['product.template'].sudo().browse(int(product_id))
        if not product.exists():
            return error_response('Product not found', 404)
        if not product.x_is_loyalty_redeemable or product.x_loyalty_points_cost <= 0:
            return error_response('Product is not redeemable with points', 400)

        points_cost = product.x_loyalty_points_cost

        program = request.env['loyalty.program'].sudo().search(
            [('program_type', '=', 'loyalty')], limit=1,
        )
        if not program:
            return error_response('No loyalty program configured', 500)

        card = request.env['loyalty.card'].sudo().search([
            ('partner_id', '=', partner.id),
            ('program_id', '=', program.id),
        ], limit=1)
        points = card.points if card else 0

        if points < points_cost:
            return error_response(
                'Not enough points: have %d, need %d' % (points, points_cost),
                400,
            )

        if not card:
            card = request.env['loyalty.card'].sudo().create({
                'partner_id': partner.id,
                'program_id': program.id,
                'points': points,
            })

        try:
            with request.env.cr.savepoint():
                card.sudo().write({'points': points - points_cost})
                redemption = request.env['mint.discount'].sudo().create_redemption(
                    partner=partner,
                    product=product,
                    points_cost=points_cost,
                )
        except Exception as e:
            _logger.exception('Redemption failed for partner %s product %s', partner.id, product_id)
            return error_response('Redemption failed: %s' % str(e), 500)

        _logger.info(
            'Loyalty redemption: partner=%s product=%s code=%s -%d pts',
            partner.id, product.id, redemption.redemption_code, points_cost,
        )

        return json_response({
            'success': True,
            'redemption': _serialize_redemption(redemption),
            'product': {
                'id': product.id,
                'name': product.name,
                'brand': product.brand_id.name if product.brand_id else None,
                'category': product.master_category or (product.categ_id.name if product.categ_id else None),
                'image_url': ('/web/image/product.template/%d/image_256' % product.id)
                             if product.image_256 else None,
                'list_price': product.list_price,
                'dutchie_product_id': product.dutchie_product_id or None,
                'sku': product.default_code or None,
            },
            'loyalty': {
                'points': card.points,
                'program_name': program.name,
            },
        })

    @http.route('/api/v1/customer/loyalty/redemptions', type='http', auth='none',
                methods=['GET', 'OPTIONS'], csrf=False, cors='*')
    def list_redemptions(self, **kw):
        """List the current user's active (pending) redemption codes."""
        if request.httprequest.method == 'OPTIONS':
            return json_response({})

        user = _verify_and_get_user()
        if not user:
            return error_response('Authentication required', 401)
        if not user.partner_id:
            return error_response('No customer profile linked to this account', 400)

        redemptions = request.env['mint.discount'].sudo().search([
            ('discount_type', '=', 'loyalty_redemption'),
            ('redemption_partner_id', '=', user.partner_id.id),
            ('redemption_status', '=', 'pending'),
        ], order='create_date desc')

        return json_response({
            'redemptions': [_serialize_redemption(r) for r in redemptions],
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
        # NOTE: `mobile` was removed from res.partner in Odoo 19; clients
        # may still send it but we silently drop the field. `preferred_store_id`
        # is also a no-op until/unless x_preferred_store_id is added back to
        # res.partner — currently only x_home_store_id exists.

        if vals:
            partner.write(vals)

        return json_response({
            'message': 'Profile updated',
            'profile': {
                'id': partner.id,
                'name': partner.name,
                'email': partner.email,
                'phone': partner.phone or '',
            },
        })

    @http.route('/api/v1/customer/preferences', type='http', auth='none',
                methods=['GET', 'PUT', 'OPTIONS'], csrf=False, cors='*')
    def customer_preferences(self, **kw):
        """Read/write the customer's SMS communication preferences (MR-1250).

        GET → { "preferences": { "sms": {
                  "marketing":     {"opted_in": bool, "date": iso|null},
                  "transactional": {"opted_in": bool, "date": iso|null},
                  "opted_out": bool } } }

        PUT accepts { "sms": {"marketing": bool, "transactional": bool} };
        either key may be omitted to leave that category unchanged. Grants
        go through res.partner.set_sms_consent (stamps date + source
        'external_web', clears a prior STOP, adds the whitelist tag);
        revocations through clear_sms_consent (grant history retained).

        The consent fields live in mint_sms_telnyx, which this module does
        not depend on — probe partner._fields rather than assume.
        """
        if request.httprequest.method == 'OPTIONS':
            return json_response({})

        user = _verify_and_get_user()
        if not user:
            return error_response('Authentication required', 401)
        if not user.partner_id:
            return error_response('No customer profile linked to this account', 400)

        partner = user.partner_id.sudo()
        if 'sms_consent_marketing' not in partner._fields:
            return error_response('SMS preferences unavailable', 503)

        if request.httprequest.method == 'PUT':
            try:
                data = json.loads(request.httprequest.data)
            except (json.JSONDecodeError, TypeError):
                return error_response('Invalid JSON body')
            sms = data.get('sms')
            if not isinstance(sms, dict):
                return error_response('Body must carry an "sms" object')
            for category in ('marketing', 'transactional'):
                if category not in sms:
                    continue
                wanted = sms[category]
                if not isinstance(wanted, bool):
                    return error_response('"%s" must be a boolean' % category)
                if wanted:
                    partner.set_sms_consent(category, source='external_web')
                else:
                    partner.clear_sms_consent(category)

        def _pref(category):
            date = partner['sms_consent_%s_date' % category]
            return {
                'opted_in': bool(partner['sms_consent_%s' % category]),
                'date': date.isoformat() if date else None,
            }

        return json_response({
            'preferences': {
                'sms': {
                    'marketing': _pref('marketing'),
                    'transactional': _pref('transactional'),
                    'opted_out': bool(partner.sms_opt_out),
                },
            },
        })
