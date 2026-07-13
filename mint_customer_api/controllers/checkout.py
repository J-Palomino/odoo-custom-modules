# -*- coding: utf-8 -*-
"""
Checkout & Loyalty endpoints for MintDeals seamless checkout.

Provides:
  - /api/v1/checkout/loyalty  — Look up customer loyalty by phone/email
  - /api/v1/checkout/create   — Create draft order (before payment)
  - /api/v1/checkout/status   — Poll order payment status
  - /api/v1/checkout/order    — Record completed order + award loyalty points

These endpoints use API key auth (X-Api-Key header) rather than JWT,
since checkout can happen for guest customers.
"""
import hmac
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
    # Constant-time compare to avoid a timing side-channel (Odoo #675).
    return bool(key and expected and hmac.compare_digest(key, expected))


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
                'phone': partner.phone or '',
                'total_spend': getattr(partner, 'x_dutchie_total_spend', 0) or 0,
                'visit_count': getattr(partner, 'x_dutchie_visit_count', 0) or 0,
            },
            'loyalty': loyalty,
        })

    # ── Create Draft Order ──────────────────────────────────────────────

    @http.route('/api/v1/checkout/create', type='http', auth='none',
                methods=['POST', 'OPTIONS'], csrf=False, cors='*')
    def create_draft_order(self, **kw):
        """Create a draft online order before payment.

        Request body:
            {
              "customer": {
                "phone": "6025551234",
                "email": "user@example.com",
                "first_name": "John",
                "last_name": "Doe",
                "birth_date": "1990-01-15"
              },
              "store_slug": "mint-tempe",
              "dutchie_checkout_id": "uuid",
              "payment_method": "online" | "in-store",
              "order_type": "PICKUP",
              "items": [
                {
                  "product_name": "Blue Dream 3.5g",
                  "product_id": "11485249",
                  "quantity": 1,
                  "unit_price": 25.00,
                  "discount": 0
                }
              ],
              "totals": {
                "subtotal": 55.00,
                "discounts": 0,
                "total": 55.00
              },
              "notes": ""
            }

        Returns:
            {
              "success": true,
              "order_id": 42,
              "order_ref": "MINT-00042",
              "status": "pending"
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
        payment_method = data.get('payment_method', 'online')

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
                # Mark consumer-origin partners so the web-customer record rules
                # hide their PII from regular staff (vendors/employees), matching
                # the /auth/register flow. Only set on NEWLY created partners — a
                # found partner may legitimately be a vendor/employee.
                'is_web_customer': True,
            })
            _logger.info('Created new customer partner %s: %s', partner.id, name)

        # Create sale order in draft state
        store_slug = data.get('store_slug', '')
        company = request.env['res.company'].sudo().search(
            [('x_slug', '=', store_slug)], limit=1,
        ) if store_slug else request.env.company

        if not company:
            company = request.env.company

        # Use with_company to set proper context (pricelist, warehouse, etc.)
        SaleOrder = request.env['sale.order'].sudo().with_company(company)

        order_vals = {
            'partner_id': partner.id,
            'company_id': company.id,
            'user_id': request.env.ref('base.user_admin').id,
            'client_order_ref': data.get('dutchie_checkout_id', ''),
            'note': data.get('notes', ''),
            'x_payment_method': payment_method,
            'x_dutchie_checkout_id': data.get('dutchie_checkout_id', ''),
            'x_checkout_status': 'pending' if payment_method == 'online' else 'pay_at_store',
        }

        order = SaleOrder.create(order_vals)

        # Add order lines
        OrderLine = request.env['sale.order.line'].sudo().with_company(company)
        for item in items:
            OrderLine.create({
                'order_id': order.id,
                'name': item.get('product_name', 'Product'),
                'product_uom_qty': item.get('quantity', 1),
                'price_unit': item.get('unit_price', 0),
                'discount': self._calc_discount_pct(
                    item.get('unit_price', 0),
                    item.get('discount', 0),
                ),
            })

        _logger.info(
            'Draft order %s created for %s (%s) — payment: %s',
            order.name, partner.name, store_slug, payment_method,
        )

        return json_response({
            'success': True,
            'order_id': order.id,
            'order_ref': order.name,
            'status': order.x_checkout_status or 'pending',
        })

    # ── Order Status Poll ─────────────────────────────────────────────

    @http.route('/api/v1/checkout/status', type='http', auth='none',
                methods=['GET', 'OPTIONS'], csrf=False, cors='*')
    def checkout_status(self, **kw):
        """Poll order payment status by Dutchie checkout ID.

        Query params:
            checkout_id — The Dutchie checkout UUID

        Returns:
            { "status": "pending" | "paid" | "pay_at_store" | "cancelled" | "not_found" }
        """
        if request.httprequest.method == 'OPTIONS':
            return json_response({})

        if not _verify_api_key():
            return error_response('Invalid API key', 401)

        checkout_id = kw.get('checkout_id', '')
        if not checkout_id:
            return error_response('checkout_id is required')

        order = request.env['sale.order'].sudo().search([
            ('x_dutchie_checkout_id', '=', checkout_id),
        ], limit=1)

        if not order:
            return json_response({'status': 'not_found'})

        return json_response({
            'status': order.x_checkout_status or 'pending',
            'order_id': order.id,
            'order_ref': order.name,
        })

    # ── Order Completion (legacy — still used for post-payment sync) ──

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

        # Quick path: mark existing order as paid (called by status poll)
        if data.get('mark_paid') and data.get('dutchie_checkout_id'):
            order = request.env['sale.order'].sudo().search([
                ('x_dutchie_checkout_id', '=', data['dutchie_checkout_id']),
            ], limit=1)
            if order and order.x_checkout_status != 'paid':
                order.sudo().write({'x_checkout_status': 'paid'})
                # Deduct any redemption + consume pending rewards. Earned
                # points are NOT credited here — the nightly Dutchie import
                # is the single award source (see _award_loyalty docstring).
                if order.partner_id:
                    self._award_loyalty(
                        order.partner_id,
                        order.amount_untaxed,
                        data.get('loyalty_points_redeemed', 0),
                        company=order.company_id,
                    )
                _logger.info('Order %s marked as paid', order.name)
                return json_response({
                    'success': True,
                    'order_id': order.id,
                    'order_ref': order.name,
                    'status': 'paid',
                })
            return json_response({'success': True, 'status': 'already_paid' if order else 'not_found'})

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
                # Mark consumer-origin partners so the web-customer record rules
                # hide their PII from regular staff (vendors/employees), matching
                # the /auth/register flow. Only set on NEWLY created partners — a
                # found partner may legitimately be a vendor/employee.
                'is_web_customer': True,
            })
            _logger.info('Created new customer partner %s: %s', partner.id, name)

        # Loyalty: deduct redeemed points immediately (they must not be
        # re-spendable), but do NOT add earned points here. The nightly Dutchie
        # transaction import (GKE dutchie-daily-import → mint.dutchie.purchase)
        # is the SINGLE award source, keyed on the POS receipt — every fulfilled
        # web pickup order also lands as a POS transaction, so awarding here too
        # double-counted. points_earned below is a PENDING estimate for display.
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

            # Pending estimate: 1 point per dollar (pre-tax subtotal minus
            # discounts). Actually credited by the nightly import.
            spend_basis = totals.get('subtotal', 0) - totals.get('discounts', 0)
            points_earned = int(spend_basis)

            if points_redeemed:
                card.sudo().write({'points': max(card.points - points_redeemed, 0)})

            _logger.info(
                'Loyalty checkout for %s: -%d redeemed now, +%d pending (awarded by nightly import)',
                partner.name, points_redeemed, points_earned,
            )

            # Auto-consume any pending /rewards redemption whose product
            # appears as a free line in this order (strict match).
            consumed = request.env['mint.discount'].sudo().consume_pending_redemption(
                partner, order_items=items,
                store=(request.env['res.company'].sudo().browse(int(data['store_id']))
                       if data.get('store_id') else None),
            )
            if consumed:
                _logger.info(
                    'Auto-consumed redemption %s for %s on order completion',
                    consumed.redemption_code, partner.name,
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

    def _calc_discount_pct(self, unit_price, discount_amount):
        """Convert flat discount amount to percentage for sale.order.line."""
        if not unit_price or unit_price <= 0 or not discount_amount:
            return 0
        return min(round((discount_amount / unit_price) * 100, 2), 100)

    def _award_loyalty(self, partner, spend_amount, points_redeemed=0, company=None):
        """Deduct redeemed points + auto-consume any pending redemption.

        Earned points are NOT credited here. The nightly Dutchie transaction
        import (GKE CronJob dutchie-daily-import → mint.dutchie.purchase,
        which awards atomically on create, deduped by POS receipt) is the
        single source of point EARNING — every fulfilled web pickup order also
        posts as a POS transaction, so crediting at checkout double-counted.

        Points from explicit checkout-time redemption (points_redeemed arg)
        are still deducted immediately so they cannot be re-spent before the
        nightly import runs. Any pending loyalty_redemption record for this
        partner is auto-marked 'used' here — points for those were already
        deducted on /rewards redeem.

        Returns the pending points estimate (1 pt / $1) for display.
        """
        loyalty_program = request.env['loyalty.program'].sudo().search(
            [('program_type', '=', 'loyalty')], limit=1
        )
        if not loyalty_program:
            return 0

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

        points_earned = int(spend_amount)
        if points_redeemed:
            card.sudo().write({'points': max(card.points - points_redeemed, 0)})

        _logger.info(
            'Loyalty checkout for %s: -%d redeemed now, +%d pending (awarded by nightly import)',
            partner.name, points_redeemed, points_earned,
        )

        consumed = request.env['mint.discount'].sudo().consume_pending_redemption(
            partner, store=company)
        if consumed:
            _logger.info(
                'Auto-consumed redemption %s (%s) for %s on order completion',
                consumed.redemption_code, consumed.redemption_reward_id.display_name,
                partner.name,
            )

        return points_earned

    def _find_partner(self, phone, email):
        """Find partner by phone or email. Returns first match."""
        Partner = request.env['res.partner'].sudo()
        partner = None

        if phone and len(phone) >= 10:
            partner = Partner.search([
                ('phone', 'ilike', phone[-10:]),
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


