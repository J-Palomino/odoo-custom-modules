# -*- coding: utf-8 -*-
"""Customer-facing gift card endpoints.

The button these serve sits at the payment step, and that placement is the
whole design. A gift card draw has to be for `min(balance, what the basket
owes)`, so it can only be decided once the basket is final — and Dutchie
exposes no "about to pay" event we could subscribe to. Rather than guess the
moment by polling and watching the total stop changing, the customer tells us
by tapping. One tap, at exactly the right instant, and no heuristic moving
real money.

Two endpoints:

    GET  /api/v1/customer/gift-cards       what they hold, and what is left
    POST /api/v1/customer/gift-card/draw   spend some of it on this basket

`dry_run` on the draw resolves everything and stops before any write, so the
screen can say "this will use $30 of your $70" before the customer commits.
"""
import json
import logging

from odoo import http
from odoo.http import request

from odoo.addons.mint_customer_api.controllers.auth import (
    json_response, error_response, unexpected_error_response,
    _verify_and_get_user,
)

_logger = logging.getLogger(__name__)


def _caller():
    """The authenticated customer, or an error response."""
    user = _verify_and_get_user()
    if not user:
        return None, error_response('Authentication required', 401)
    if not user.partner_id:
        return None, error_response('No customer profile linked to this account', 400)
    return user, None


def _serialize(card):
    return {
        'code': card.code,
        'balance': round(card.balance, 2),
        'face_value': round(card.face_value, 2),
        'settled': round(card.settled_amount, 2),
        'on_hold': round(card.held_amount, 2),
        'state': card.state,
        'spendable': card.is_spendable,
        'expires_at': card.expires_at.isoformat() if card.expires_at else None,
    }


class MintGiftCardController(http.Controller):

    @http.route('/api/v1/customer/gift-cards', type='http', auth='none',
                methods=['GET', 'OPTIONS'], csrf=False, cors='*')
    def list_gift_cards(self, **kw):
        """Cards this customer holds. Empty list is a normal answer."""
        if request.httprequest.method == 'OPTIONS':
            return json_response({})
        user, denied = _caller()
        if denied:
            return denied
        try:
            cards = request.env['mint.gift.card'].sudo().search([
                ('partner_id', '=', user.partner_id.id),
                ('state', 'in', ['active', 'depleted']),
            ], order='balance desc, id desc')
            return json_response({'cards': [_serialize(c) for c in cards]})
        except Exception:
            return unexpected_error_response(
                "We couldn't load your gift cards.",
                'gift-cards list failed for partner %s' % user.partner_id.id)

    @http.route('/api/v1/customer/gift-card/draw', type='http', auth='none',
                methods=['POST', 'OPTIONS'], csrf=False, cors='*')
    def draw_gift_card(self, **kw):
        """Spend part of a gift card on the customer's live transaction.

        Body: {code?, store_slug, shipment_id, dry_run?, idempotency_key?}

        `code` is optional — with none given we use the caller's own card with
        the largest balance, which is what someone tapping "use my gift card"
        means. A code is accepted so a customer can spend one they were given
        without a separate claim step; see the ownership rule below.
        """
        if request.httprequest.method == 'OPTIONS':
            return json_response({})
        user, denied = _caller()
        if denied:
            return denied

        try:
            data = json.loads(request.httprequest.data or '{}')
        except (json.JSONDecodeError, TypeError):
            return error_response('Invalid JSON body', 400)

        store_slug = (data.get('store_slug') or '').strip()
        shipment_id = data.get('shipment_id')
        if not store_slug or not shipment_id:
            return error_response('store_slug and shipment_id are required', 400)

        Card = request.env['mint.gift.card'].sudo()
        partner = user.partner_id

        code = (data.get('code') or '').strip().upper()
        if code:
            card = Card.search([('code', '=', code)], limit=1)
            if not card:
                # 404 rather than a distinguishable "wrong code" — a caller
                # should not be able to probe which codes exist.
                return error_response('Gift card not found', 404)
            # Ownership: a card bound to someone else is off limits. An
            # UNBOUND card is a bearer instrument, so the first customer to
            # spend it takes ownership — the same transfer-on-use rule the
            # promo gift links already follow.
            if card.partner_id and card.partner_id.id != partner.id:
                return error_response('Gift card not found', 404)
        else:
            card = Card.search([
                ('partner_id', '=', partner.id),
                ('state', '=', 'active'),
                ('is_spendable', '=', True),
            ], order='balance desc, id desc', limit=1)
            if not card:
                return error_response('No gift card available on this account', 404)

        store = request.env['res.company'].sudo().search([
            ('slug', '=', store_slug),
            ('dutchie_pos_location_id', '!=', False),
        ], limit=1)
        if not store:
            return error_response('Unknown store', 404)
        push = request.env['mint.ptl.day'].sudo()
        loc_id = push._resolve_pos_loc_id(store)
        lsp_id = push._resolve_lsp_id(store)
        if not loc_id or not lsp_id:
            return error_response('That store is not wired to a register', 409)

        try:
            if data.get('dry_run'):
                # Resolves the basket and the amount, writes nothing. This is
                # what lets the screen say "uses $30 of your $70" before the
                # customer commits to anything.
                result = card.plan_draw(loc_id, lsp_id, shipment_id,
                                        customer_id=data.get('customer_id'),
                                        register=data.get('register'))
            else:
                result = card.execute_draw(
                    loc_id, lsp_id, shipment_id,
                    customer_id=data.get('customer_id'),
                    register=data.get('register'),
                    # Scoped per (card, shipment) by default so an impatient
                    # double-tap replays rather than drawing twice.
                    idempotency_key=(data.get('idempotency_key')
                                     or 'ship:%s' % shipment_id),
                )
                if result.get('ok') and not card.partner_id:
                    # Bearer card just spent — bind it, so nobody else can.
                    card.write({'partner_id': partner.id})
        except Exception:
            return unexpected_error_response(
                "We couldn't use your gift card on this order.",
                'gift card draw failed for %s on shipment %s' % (card.code, shipment_id))

        if not result.get('ok'):
            # A refusal is the customer's situation (empty basket, dead card,
            # register declined), not a fault: 409 with a stable error key the
            # UI can branch on. `mint_failed` is the exception — that carries a
            # backend exception's text, which must never reach a shopper, so it
            # gets a written sentence instead. The detail is already in the log.
            err = result.get('error')
            safe = {
                'mint_failed': "We couldn't set up that payment. Nothing was "
                               "charged to your card — please try again.",
            }
            if err == 'mint_failed':
                _logger.error('gift card %s mint failed: %s',
                              card.code, result.get('message'))
            return json_response({
                'ok': False,
                'error': err,
                'message': safe.get(err) or result.get('message'),
                'card': _serialize(card),
            }, status=409)

        return json_response({
            'ok': True,
            'dry_run': bool(data.get('dry_run')),
            'amount': round(result.get('amount') or 0.0, 2),
            'covers_basket': result.get('covers_basket'),
            'balance_after': round(result.get('balance_after')
                                   if result.get('balance_after') is not None
                                   else card.balance, 2),
            'replayed': result.get('replayed', False),
            'card': _serialize(card),
        })
