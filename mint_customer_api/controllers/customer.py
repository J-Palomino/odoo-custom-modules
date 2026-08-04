# -*- coding: utf-8 -*-
"""
Customer profile endpoints for MintDeals frontend.

All endpoints require JWT authentication via Authorization header.
"""
import base64
import hashlib
import json
import logging

from odoo import fields, http
from odoo.http import request, Response

from .auth import (json_response, error_response, unexpected_error_response,
                   _verify_and_get_user)

_logger = logging.getLogger(__name__)

# md5 of Odoo's stock grey "no image" PNG, per resized field. The Dutchie sync
# has historically downloaded this placeholder and stored the bytes verbatim,
# so an image field being *set* does not mean a real photo is present — the
# bytes have to be compared. Sizes differ because Odoo re-encodes per field.
_PLACEHOLDER_IMAGE_MD5 = frozenset({
    'ed4046b72d8bf5add8585af91355734c',  # image_1920 / image_512 / image_256
    'e636a30b8a5a8ca4bbab480eafef8977',  # image_128
})

# Leading magic bytes -> mimetype. Stored product thumbnails are a mix of PNG
# and JPEG, so the type must be sniffed rather than assumed.
_IMAGE_MAGIC = (
    (b'\xff\xd8\xff', 'image/jpeg'),
    (b'\x89PNG\r\n\x1a\n', 'image/png'),
    (b'GIF87a', 'image/gif'),
    (b'GIF89a', 'image/gif'),
)

# Path of the public thumbnail route below, used to build image_url values.
_REDEEMABLE_IMAGE_PATH = '/api/v1/customer/loyalty/redeemables/%d/image'


def _is_placeholder_image(b64_value):
    """True when a base64 image field holds Odoo's grey placeholder."""
    try:
        raw = base64.b64decode(b64_value)
    except (TypeError, ValueError):
        return False
    return hashlib.md5(raw).hexdigest() in _PLACEHOLDER_IMAGE_MD5


def _guess_image_mimetype(raw):
    """Sniff an image mimetype from its magic bytes."""
    for magic, mimetype in _IMAGE_MAGIC:
        if raw.startswith(magic):
            return mimetype
    if raw[:4] == b'RIFF' and raw[8:12] == b'WEBP':
        return 'image/webp'
    return 'application/octet-stream'


def _redeemable_image_url(product):
    """Public image URL for a redeemable product, or None if it has no photo.

    Prefers Odoo's stored 256px thumbnail, served via the dedicated route
    below rather than `/web/image`: these product.template records are not
    website-published, so `/web/image` returns the grey placeholder (HTTP 200)
    to anonymous visitors even though real bytes are on the record.

    Falls back to the Dutchie CDN URL in `x_image_url`, which is public but
    full-resolution (megabytes), so it is only used when no thumbnail exists.
    """
    thumbnail = product.image_256
    if thumbnail and not _is_placeholder_image(thumbnail):
        return _REDEEMABLE_IMAGE_PATH % product.id
    return product.x_image_url or None


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

        The DiscountCode is shown on /rewards as a short typeable code (the
        budtender keys it into the register), which applies the Dutchie
        ApplicationMethodId=3 discount our pipeline created in Backoffice.
        Read-only.
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
        """List products flagged as points-redeemable, scoped to one location.

        Query params: `store_id` (res.company id) or `store_slug` (x_slug).

        This used to return every flagged product to every caller, which was
        wrong in a way customers could see: rewards are printed per state, the
        catalogs differ completely between them, and a redemption is minted
        against ONE product id and locked to the redeeming store's region. A
        Florida customer shown an Arizona product could spend points on a coupon
        no register of theirs would ever accept.

        Scoping uses the link the product data already carries:
            product.template.x_location_id -> res.company.dutchie_store_id

        A store resolves to its whole region, so a customer sees what any store
        in their state carries -- matching the redemption lock, which is
        region-wide (create_redemption sets store_ids to the region's stores).

        Resolution order, most specific first:

          1. `store_id` / `store_slug` query param — an explicit choice.
          2. `region` — a region slug or code ("arizona" / "AZ"). The catalog is
             region-wide anyway, so a region is all this endpoint actually needs;
             demanding a specific store was stricter than the data requires.
          3. The SIGNED-IN customer's saved store (res.partner.x_home_store_id).

        (3) is what makes this durable. (1) and (2) both depend on the client
        passing something it learned from localStorage, which is per-device,
        cleared with site data, and absent on a first visit — so a customer who
        arrived straight at /rewards on a new phone saw an empty grid. Only 24
        of 533 portal users had a saved store when this was written, because
        nothing but the POS sync ever wrote one; PUT /customer/profile now
        accepts it, so a customer's choice persists server-side and follows them
        to any device.

        With none of the three the list is EMPTY rather than global. A caller we
        cannot place must not be shown a state's catalog, and showing the union
        would recreate the cross-state leak. States with no catalog yet (FL, MI)
        therefore return empty, which is correct: nothing is redeemable there
        until their rewards are set up.
        """
        if request.httprequest.method == 'OPTIONS':
            return json_response({})

        Company = request.env['res.company'].sudo()
        store = Company.browse(int(kw['store_id'])).exists() \
            if str(kw.get('store_id') or '').isdigit() else Company
        if not store and kw.get('store_slug'):
            store = Company.search([('x_slug', '=', kw['store_slug'])], limit=1)
        resolved_from = 'param' if store else None

        # A region is enough: the catalog below is region-wide regardless of
        # which store in it we resolve to.
        region_hint = (kw.get('region') or '').strip()
        if not store and region_hint:
            reg = request.env['mint.region'].sudo().search(
                ['|', ('slug', '=ilike', region_hint), ('code', '=ilike', region_hint)], limit=1)
            if reg:
                store = Company.search([('region_id', '=', reg.id),
                                        ('is_dispensary', '=', True)], limit=1)
                resolved_from = 'region' if store else None

        # Saved store on the customer record — survives new devices and cleared
        # storage. Auth is optional on this route (the catalog is not private),
        # so an anonymous caller just skips this step.
        if not store:
            user = _verify_and_get_user()
            home = user and user.partner_id and getattr(
                user.partner_id.sudo(), 'x_home_store_id', False)
            if home:
                store = home.sudo()
                resolved_from = 'saved_store'

        if not store:
            return json_response({'products': [], 'store_id': None, 'region': None,
                                  'resolved_from': None,
                                  'reason': 'no store specified'})

        # The region's stores -- same set create_redemption locks a coupon to.
        region = store.region_id
        siblings = Company.search([('region_id', '=', region.id),
                                   ('is_dispensary', '=', True)]) if region else store
        uuids = [u for u in siblings.mapped('dutchie_store_id') if u]
        if not uuids:
            return json_response({'products': [], 'store_id': store.id,
                                  'region': region.code if region else None,
                                  'reason': 'store has no dutchie_store_id'})

        products = request.env['product.template'].sudo().search([
            ('x_is_loyalty_redeemable', '=', True),
            ('x_loyalty_points_cost', '>', 0),
            ('active', '=', True),
            ('x_location_id', 'in', uuids),
        ], order='x_loyalty_points_cost asc, name asc')

        # Odoo carries many duplicate product.template rows per physical
        # product — one WYLD gummy resolves to 18 templates covering just 2
        # SKUs — and they stayed invisible until the catalogue was bulk-flagged
        # redeemable. Measured on Tempe: 3,656 rows for 644 real products, so
        # 82% of the grid was the same items repeated, and 82% of a 1.06 MB
        # response was redundant. Collapse on dutchie_product_id, the canonical
        # product key that every redeemable row carries.
        #
        # The tie-break matters. Duplicates never disagree on points_cost
        # (checked across all 549 duplicated groups on prod), so the pick cannot
        # change what a customer is charged. They DO disagree on images: 78
        # groups carry a photo on only some rows, so choosing blindly would
        # blank out products that actually have one. Prefer a row with a usable
        # image, then the lowest id so the choice is stable between requests.
        #
        # image_url is resolved once per row here and reused in the payload —
        # it md5s the stored thumbnail to detect placeholder bytes, which is not
        # work worth doing twice per product.
        rows = [(p, _redeemable_image_url(p)) for p in products]
        best = {}
        unkeyed = []
        for product, image_url in rows:
            key = product.dutchie_product_id
            if not key:
                # No canonical key — keep the row rather than collapsing every
                # such product into a single bucket keyed on False.
                unkeyed.append((product, image_url))
                continue
            incumbent = best.get(key)
            if incumbent is None or (0 if image_url else 1, product.id) < \
                    (0 if incumbent[1] else 1, incumbent[0].id):
                best[key] = (product, image_url)

        deduped = sorted(
            list(best.values()) + unkeyed,
            key=lambda row: (row[0].x_loyalty_points_cost, row[0].name or ''),
        )

        return json_response({
            'store_id': store.id,
            'region': region.code if region else None,
            # Lets the client tell "you are seeing Arizona because you picked it"
            # from "...because it is saved on your account" — and lets us measure
            # how often the durable path is doing the work.
            'resolved_from': resolved_from,
            'products': [{
                'id': p.id,
                'name': p.name,
                'brand': p.brand_id.name if p.brand_id else None,
                'category': p.master_category or (p.categ_id.name if p.categ_id else None),
                'strain_type': p.strain_type or None,
                'image_url': image_url,
                'points_cost': p.x_loyalty_points_cost,
                'list_price': p.list_price,
                'dutchie_product_id': p.dutchie_product_id or None,
                'sku': p.default_code or None,
            } for p, image_url in deduped],
        })

    @http.route('/api/v1/customer/loyalty/redeemables/<int:product_id>/image',
                type='http', auth='none', methods=['GET'], csrf=False, cors='*')
    def redeemable_image(self, product_id, **kw):
        """Serve a redeemable product's 256px thumbnail to anonymous visitors.

        Exists because these product.template records are not website-published,
        so Odoo's own `/web/image` route answers public requests with the grey
        placeholder. Deliberately scoped to products flagged redeemable so this
        cannot be used to read arbitrary product images.
        """
        product = request.env['product.template'].sudo().browse(product_id).exists()
        if not product or not product.x_is_loyalty_redeemable:
            return request.not_found()
        if not product.image_256 or _is_placeholder_image(product.image_256):
            return request.not_found()

        raw = base64.b64decode(product.image_256)
        return request.make_response(raw, headers=[
            ('Content-Type', _guess_image_mimetype(raw)),
            ('Content-Length', str(len(raw))),
            ('Cache-Control', 'public, max-age=86400'),
        ])

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

        # Location-lock the redemption to the selected store's STATE so it can
        # only be consumed at stores in the same market (per-state separation).
        # Resolved BEFORE the deduction below, because the push gate is checked
        # against it and a blocked push must cost the customer nothing.
        store = False
        if data.get('store_id'):
            store = request.env['res.company'].sudo().browse(int(data['store_id']))
        elif data.get('store_slug'):
            store = request.env['res.company'].sudo().search(
                [('x_slug', '=', data['store_slug'])], limit=1)

        # FAIL CLOSED. The Dutchie push fails soft by design — by then the points
        # are gone and the coupon exists, so raising would cost the customer
        # both. That makes a blocked push invisible: debited, handed a code, and
        # refused at the register, with no push-log row and no retry.
        #
        # Only Arizona can push today (1 of 43 stores enabled; the other five
        # markets have the gate off) while reward catalogs are live in IL/MO/NV.
        # Refusing here keeps the points instead of selling a dead code.
        blocked = request.env['mint.discount'].sudo().redemption_push_blocked_reason(
            store or None)
        if blocked:
            _logger.error(
                'Redemption refused for partner %s at store %s: %s — points NOT '
                'deducted (a coupon that cannot reach Dutchie is a dead code)',
                partner.id, store.display_name if store else '(none)', blocked)
            return error_response(
                'Rewards cannot be redeemed at this store yet. Your points have '
                'not been used — please try again later or ask a budtender.', 409)

        try:
            with request.env.cr.savepoint():
                card.sudo().write({'points': points - points_cost})
                redemption = request.env['mint.discount'].sudo().create_redemption(
                    partner=partner,
                    product=product,
                    points_cost=points_cost,
                    store=store or None,
                )
        except Exception:
            return unexpected_error_response(
                'Could not redeem that reward right now.',
                'Redemption failed for partner %s product %s' % (partner.id, product_id),
            )

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
                'image_url': _redeemable_image_url(product),
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
        # may still send it but we silently drop the field.

        # Saved store. Until now nothing but the POS sync ever wrote
        # x_home_store_id, so only 24 of 533 portal customers had one and the
        # rest were placed purely by localStorage — per-device, cleared with
        # site data, gone on a new phone. Accepting it here is what makes a
        # customer's choice durable: /loyalty/redeemables falls back to this
        # when the client passes no store, so the rewards grid fills in on any
        # device they sign in from.
        #
        # Accepts an id or a slug. Must be a real dispensary — a bad id would
        # otherwise persist and quietly place the customer in the wrong state
        # for every future request.
        #
        # x_home_store_id lives in mint_dutchie_sync, which this module does not
        # depend on, so the field may genuinely be absent from the registry —
        # the same reason get_profile reads it through getattr. Probe rather
        # than assume: writing a field that isn't there raises.
        store_key = data.get('home_store_id', data.get('home_store_slug'))
        if store_key not in (None, '') and 'x_home_store_id' in partner._fields:
            Company = request.env['res.company'].sudo()
            store = (Company.browse(int(store_key)).exists()
                     if str(store_key).isdigit()
                     else Company.search([('x_slug', '=', str(store_key))], limit=1))
            if not store or not store.is_dispensary:
                return error_response('Unknown store: %s' % store_key)
            vals['x_home_store_id'] = store.id

        if vals:
            partner.write(vals)

        return json_response({
            'message': 'Profile updated',
            'profile': {
                'id': partner.id,
                'name': partner.name,
                'email': partner.email,
                'phone': partner.phone or '',
                # getattr for the same reason as get_profile: the field is
                # defined in mint_dutchie_sync, not a dependency of this module.
                'home_store_id': getattr(partner, 'x_home_store_id', False) and partner.x_home_store_id.id or None,
                'home_store_name': getattr(partner, 'x_home_store_id', False) and partner.x_home_store_id.name or None,
            },
        })
