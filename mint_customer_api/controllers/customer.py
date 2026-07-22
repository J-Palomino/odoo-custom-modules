# -*- coding: utf-8 -*-
"""
Customer profile endpoints for MintDeals frontend.

All endpoints require JWT authentication via Authorization header.
"""
import json
import logging

from odoo import fields, http
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

        # Internal/staff accounts don't participate in loyalty. Signal the
        # frontend so it hides the rewards UI for employee logins.
        if not user.share:
            return json_response({'internal': True})

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

    @http.route('/api/v1/customer/welcome-coupon', type='http', auth='none',
                methods=['GET', 'OPTIONS'], csrf=False, cors='*')
    def get_welcome_coupon(self, **kw):
        """Return the customer's welcome free pre-roll coupon (task #102149).

        The DiscountCode is rendered as a scannable Code128 barcode on /account;
        scanning it at the register applies the Dutchie ApplicationMethodId=3
        discount our pipeline created in Backoffice. Read-only.
        """
        if request.httprequest.method == 'OPTIONS':
            return json_response({})

        user = _verify_and_get_user()
        if not user:
            return error_response('Authentication required', 401)
        if not user.partner_id:
            return error_response('No customer profile linked to this account', 400)

        partner = user.partner_id.sudo()
        coupon = request.env['mint.discount'].sudo().search([
            ('is_welcome_preroll', '=', True),
            ('redemption_partner_id', '=', partner.id),
        ], order='id desc', limit=1)
        if not coupon:
            return json_response({'welcome_coupon': None})

        # valid_until is a Date, so this must be a date-to-date comparison.
        # Against fields.Datetime.now() Python raises TypeError ("can't compare
        # datetime.datetime to datetime.date") — and because a live coupon is
        # 'pending', neither earlier branch short-circuits, so the endpoint 500d
        # for every usable coupon and /rewards silently rendered no card
        # (the client bails on `if (!res.ok) return`).
        today = fields.Date.context_today(coupon)
        if coupon.redemption_status == 'used':
            status = 'redeemed'
        elif (coupon.redemption_status in ('expired', 'voided')
              or (coupon.valid_until and coupon.valid_until < today)):
            status = 'expired'
        else:
            status = 'active'
        return json_response({
            'welcome_coupon': {
                'code': coupon.dutchie_discount_code or '',
                'label': 'Welcome Free Pre-Roll',
                'reward': '100% off one pre-roll',
                'status': status,
                'expires_at': coupon.valid_until.isoformat() if coupon.valid_until else None,
                'in_store_only': True,
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
                # Location-lock the redemption to the selected store's STATE so
                # it can only be consumed at stores in the same market (per-state
                # separation). Resolve the store from the request body.
                store = False
                if data.get('store_id'):
                    store = request.env['res.company'].sudo().browse(int(data['store_id']))
                elif data.get('store_slug'):
                    store = request.env['res.company'].sudo().search(
                        [('x_slug', '=', data['store_slug'])], limit=1)
                redemption = request.env['mint.discount'].sudo().create_redemption(
                    partner=partner,
                    product=product,
                    points_cost=points_cost,
                    store=store or None,
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
