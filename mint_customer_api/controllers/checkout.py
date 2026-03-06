# -*- coding: utf-8 -*-
"""
Checkout & Loyalty endpoints for MintDeals seamless checkout.

Provides:
  - /api/v1/checkout/loyalty  — Look up customer loyalty by phone/email
  - /api/v1/checkout/order    — Record completed order + award loyalty points

These endpoints use API key auth (X-Api-Key header) rather than JWT,
since checkout can happen for guest customers.
"""
import json
import logging

from odoo import http
from odoo.http import request, Response

from .auth import json_response, error_response

_logger = logging.getLogger(__name__)

# Simple API key check for server-to-server calls
def _verify_api_key():
    """Verify X-Api-Key header matches the configured checkout API key."""
    key = request.httprequest.headers.get('X-Api-Key', '')
    if not key:
        return False
    expected = request.env['ir.config_parameter'].sudo().get_param(
        'mint_customer_api.checkout_api_key', ''
    )
    return key and expected and key == expected


class MintCheckout(http.Controller):
    """Checkout and loyalty controller."""

    # ── Loyalty Lookup ───────────────────────────────────────────────────

    @http.route('/api/v1/checkout/loyalty', type='http', auth='none',
                methods=['POST', 'OPTIONS'], csrf=False, cors='*')
    def loyalty_lookup(self, **kw):
        """Look up customer loyalty balance by phone or email.

        Request body:
            { "phone": "6025551234", "email": "user@example.com" }

        At least one of phone or email is required.

        Returns:
            {
              "found": true,
              "customer": { "id", "name", "email", "phone" },
              "loyalty": {
                "program_name": "Mint Rewards",
                "points": 150,
                "point_name": "Points",
                "available_rewards": [ ... ]
              }
            }
        """
        if request.httprequest.method == 'OPTIONS':
            return json_response({})

        if not _verify_api_key():
            return error_response('Invalid API key', 401)

        try:
            data = json.loads(request.httprequest.data)
        except (json.JSONDecodeError, TypeError):
            return error_response('Invalid JSON body')

        phone = _normalize_phone(data.get('phone', ''))
        email = (data.get('email') or '').strip().lower()

        if not phone and not email:
            return error_response('Phone or email is required')

        partner = self._find_partner(phone, email)

        if not partner:
            return json_response({'found': False, 'loyalty': None})

        loyalty = self._get_loyalty_info(partner)

        return json_response({
            'found': True,
            'customer': {
                'id': partner.id,
                'name': partner.name,
                'email': partner.email or '',
                'phone': partner.phone or partner.mobile or '',
                'total_spend': getattr(partner, 'x_dutchie_total_spend', 0) or 0,
                'visit_count': getattr(partner, 'x_dutchie_visit_count', 0) or 0,
            },
            'loyalty': loyalty,
        })

    # ── Order Creation ───────────────────────────────────────────────────

    @http.route('/api/v1/checkout/order', type='http', auth='none',
                methods=['POST', 'OPTIONS'], csrf=False, cors='*')
    def create_order(self, **kw):
        """Record a completed checkout order and award loyalty points.

        Request body:
            {
              "customer": {
                "phone": "6025551234",
                "email": "user@example.com",
                "first_name": "John",
                "last_name": "Doe"
              },
              "store_id": 5,  (Odoo company ID)
              "dutchie_checkout_id": "abc123",
              "order_type": "PICKUP",
              "items": [
                {
                  "product_name": "Jeeter Infused Preroll",
                  "sku": "12345",
                  "quantity": 2,
                  "unit_price": 15.00,
                  "discount": 3.00
                }
              ],
              "totals": {
                "subtotal": 30.00,
                "discounts": 3.00,
                "taxes": 2.70,
                "total": 29.70
              },
              "loyalty_points_redeemed": 0
            }
        """
        if request.httprequest.method == 'OPTIONS':
            return json_response({})

        if not _verify_api_key():
            return error_response('Invalid API key', 401)

        try:
            data = json.loads(request.httprequest.data)
        except (json.JSONDecodeError, TypeError):
            return error_response('Invalid JSON body')

        customer_data = data.get('customer', {})
        items = data.get('items', [])
        totals = data.get('totals', {})

        if not items:
            return error_response('Items are required')

        phone = _normalize_phone(customer_data.get('phone', ''))
        email = (customer_data.get('email') or '').strip().lower()

        # Find or create customer
        partner = self._find_partner(phone, email)
        if not partner:
            name = ' '.join(filter(None, [
                customer_data.get('first_name', ''),
                customer_data.get('last_name', ''),
            ])).strip() or email or phone
            partner = request.env['res.partner'].sudo().create({
                'name': name,
                'email': email or False,
                'phone': phone or False,
                'customer_rank': 1,
            })
            _logger.info('Created new customer partner %s: %s', partner.id, name)

        # Award loyalty points (1 point per dollar spent)
        points_earned = 0
        total_amount = totals.get('total', 0)
        points_redeemed = data.get('loyalty_points_redeemed', 0)

        loyalty_program = request.env['loyalty.program'].sudo().search(
            [('program_type', '=', 'loyalty')], limit=1
        )

        if loyalty_program:
            # Find or create loyalty card for this customer
            card = request.env['loyalty.card'].sudo().search([
                ('partner_id', '=', partner.id),
                ('program_id', '=', loyalty_program.id),
            ], limit=1)

            if not card:
                card = request.env['loyalty.card'].sudo().create({
                    'partner_id': partner.id,
                    'program_id': loyalty_program.id,
                    'points': 0,
                })

            # Award: 1 point per dollar (on pre-tax subtotal minus discounts)
            spend_basis = totals.get('subtotal', 0) - totals.get('discounts', 0)
            points_earned = int(spend_basis)

            # Deduct redeemed points, add earned
            new_balance = card.points - points_redeemed + points_earned
            card.sudo().write({'points': max(new_balance, 0)})

            _logger.info(
                'Loyalty update for %s: -%d redeemed, +%d earned = %d balance',
                partner.name, points_redeemed, points_earned, max(new_balance, 0)
            )

        # Update Dutchie spend/visit tracking
        partner.sudo().write({
            'x_dutchie_total_spend': (
                (getattr(partner, 'x_dutchie_total_spend', 0) or 0) + total_amount
            ),
            'x_dutchie_visit_count': (
                (getattr(partner, 'x_dutchie_visit_count', 0) or 0) + 1
            ),
        })

        return json_response({
            'success': True,
            'customer_id': partner.id,
            'loyalty': {
                'points_earned': points_earned,
                'points_redeemed': points_redeemed,
                'new_balance': max((card.points if loyalty_program else 0), 0),
            },
            'dutchie_checkout_id': data.get('dutchie_checkout_id'),
        })

    # ── Helpers ──────────────────────────────────────────────────────────

    def _find_partner(self, phone, email):
        """Find partner by phone or email. Returns first match."""
        Partner = request.env['res.partner'].sudo()
        partner = None

        if phone:
            partner = Partner.search([
                '|', ('phone', 'ilike', phone[-10:]),
                ('mobile', 'ilike', phone[-10:]),
            ], limit=1)

        if not partner and email:
            partner = Partner.search([
                ('email', '=ilike', email),
            ], limit=1)

        return partner

    def _get_loyalty_info(self, partner):
        """Get loyalty program info for a partner."""
        program = request.env['loyalty.program'].sudo().search(
            [('program_type', '=', 'loyalty')], limit=1
        )
        if not program:
            return None

        card = request.env['loyalty.card'].sudo().search([
            ('partner_id', '=', partner.id),
            ('program_id', '=', program.id),
        ], limit=1)

        points = card.points if card else 0

        # Get available rewards
        rewards = []
        for reward in program.reward_ids:
            if points >= reward.required_points:
                rewards.append({
                    'id': reward.id,
                    'name': reward.display_name,
                    'type': reward.reward_type,
                    'required_points': reward.required_points,
                    'discount': reward.discount if reward.reward_type == 'discount' else None,
                    'discount_max': reward.discount_max_amount,
                })

        return {
            'program_name': program.name,
            'points': points,
            'point_name': program.portal_point_name or 'Points',
            'card_id': card.id if card else None,
            'available_rewards': rewards,
        }


def _normalize_phone(phone):
    """Strip phone to digits only, return last 10."""
    if not phone:
        return ''
    digits = ''.join(c for c in str(phone) if c.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits
