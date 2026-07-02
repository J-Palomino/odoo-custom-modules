# -*- coding: utf-8 -*-
"""
Extend mint.discount with Odoo → Redis push hooks.

Emits a mintinvsvc-compatible discount shape per store on any change to a
Redis-relevant field. Preserves Dutchie join keys: when serializing, the
m2m FKs (brand_ids/category_ids/product_ids) are dereferenced back to
Dutchie IDs so mintinvsvc's inventory-matching (which keys off Dutchie
IDs) continues to work without a schema change.

Restriction-ID fallback: if the raw *_restriction_ids_raw Char field is
set but the m2m FK isn't populated yet (Dutchie deals sync fast but the
product mirror may lag), we emit the raw Dutchie IDs directly. That
keeps matching functional during the Dutchie-mirror backfill window.
"""
import logging

from odoo import api, fields, models, _
from odoo.addons.mint_api_v2.models.discount_canonical import (
    CALC_METHOD_BY_ID,
    discount_value_for,
    parse_raw_restriction,
)

_logger = logging.getLogger(__name__)

# Fields whose change should trigger a Redis push. Anything else (description,
# terms, image, chatter, loyalty-redemption internals) doesn't affect the
# storefront rendering or the price-calc inputs — skip the push for quiet edits.
REDIS_RELEVANT_FIELDS = frozenset({
    # Identity
    'name', 'code',
    # Status
    'is_active', 'is_published', 'is_available_online',
    # Validity
    'valid_from', 'valid_until', 'start_time', 'end_time',
    'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday',
    # Reward config (discount_percent dropped — orphaned, always 0.0, unconsumed)
    'discount_type', 'discount_amount',
    'calculation_method_id', 'discount_value', 'highest_or_lowest',
    'apply_to_only_one_item', 'item_group_type_id',
    # Thresholds
    'threshold_type', 'threshold_min', 'threshold_max', 'minimum_items_required',
    # Targeting (FKs — the resolved truth)
    'store_ids', 'brand_ids', 'category_ids', 'product_ids',
    'exclude_brand_ids', 'exclude_category_ids', 'exclude_product_ids',
    'weight_ids', 'exclude_weight_ids',
    'vendor_ids', 'exclude_vendor_ids',
    'excluded_skus',
    # Targeting (raw — fallback when FKs not yet resolved)
    'brand_restriction_ids_raw', 'category_restriction_ids_raw',
    'product_restriction_ids_raw', 'inventory_tag_ids_raw',
    # Application
    'application_method', 'can_stack_automatically', 'first_time_customer_only',
    'first_time_customer_only',
    # Display
    'menu_display_name', 'menu_display_image_url', 'menu_display_rank',
    'online_name',
})

# CalculationMethodId → canonical calculation_method string, sourced from the
# shared registry (mint_api_v2/data/discount-canonical.json). The previous
# inline map here was INVERTED on ids 1/2/15 (1→PERCENT_OFF instead of
# FLAT_AMOUNT_OFF, 2→FIXED_AMOUNT_OFF instead of PERCENT_OFF, 15→PRICE_TO_AMOUNT_TOTAL
# instead of LOYALTY), so every Odoo-pushed percent deal emitted the wrong method
# and silently stopped applying on the storefront. Live-verified 2026-06-05.
CALC_METHOD_ID_TO_STRING = CALC_METHOD_BY_ID


# Shared parser now lives in mint_api_v2.discount_canonical so the Dutchie push
# (mint_command_center) can reuse the exact same raw-restriction handling.
_parse_raw_restriction = parse_raw_restriction


class MintDiscount(models.Model):
    _inherit = 'mint.discount'

    # ── Hooks ─────────────────────────────────────────────────────────

    @api.model_create_multi
    def create(self, vals_list):
        recs = super().create(vals_list)
        recs._enqueue_redis_push(changed_fields=None)
        return recs

    def write(self, vals):
        # Capture affected store set *before* the write — a store_ids change
        # can remove a company, and we still want that company's push so its
        # Redis cache drops the now-unlinked deal.
        affected_before = {sid for rec in self for sid in rec.store_ids.ids}
        res = super().write(vals)
        if vals.keys() & REDIS_RELEVANT_FIELDS:
            affected_after = {sid for rec in self for sid in rec.store_ids.ids}
            self._enqueue_redis_push(
                changed_fields=set(vals.keys()),
                extra_company_ids=affected_before | affected_after,
            )
        return res

    def unlink(self):
        affected = {sid for rec in self for sid in rec.store_ids.ids}
        res = super().unlink()
        if affected:
            self.env['mint.redis.push.queue'].sudo()._enqueue_discount_locations(affected)
        return res

    # ── Push orchestration ────────────────────────────────────────────

    def _enqueue_redis_push(self, changed_fields=None, extra_company_ids=None):
        """Figure out which stores need a push and enqueue them."""
        companies = set(extra_company_ids or [])
        for rec in self:
            companies.update(rec.store_ids.ids)
        if companies:
            self.env['mint.redis.push.queue'].sudo()._enqueue_discount_locations(
                list(companies)
            )

    @api.model
    def _push_location_to_redis(self, company_id):
        """Full-replace the discount set in Redis for one location.

        Emits every discount linked to this company that's is_active=True,
        regardless of source (dutchie/ptl/manual). mintinvsvc's Redis cache
        for this location_uuid becomes the canonical union, with Odoo as
        the source of truth."""
        Company = self.env['res.company'].sudo()
        company = Company.browse(company_id)
        if not company.exists() or not company.dutchie_store_id:
            _logger.warning('Skip push for company %s: no dutchie_store_id', company_id)
            return
        uuid = company.dutchie_store_id

        discounts = self.sudo().search([
            ('store_ids', 'in', company_id),
            ('is_active', '=', True),
        ])
        payload = [d._serialize_for_redis(uuid) for d in discounts]

        # allow_empty=True: Odoo is the authoritative set. A push with zero
        # discounts is a legitimate "this store has no active deals right now"
        # statement, not a bug. The mintinvsvc safety rail still blocks empty
        # pushes from other clients (curl, misbehaving scripts).
        Queue = self.env['mint.redis.push.queue'].sudo()
        status, body = Queue._post(
            f'/api/locations/{uuid}/discounts/full-replace',
            {'discounts': payload, 'source': 'odoo', 'allow_empty': True},
        )
        if status >= 300:
            raise RuntimeError(f'push got {status}: {body[:200]}')
        _logger.info('Pushed %d discounts to Redis for %s (HTTP %s)',
                     len(payload), company.name, status)

    # ── Serialization ─────────────────────────────────────────────────

    def _serialize_for_redis(self, location_uuid):
        """Emit one discount in mintinvsvc's expected shape.

        Source-of-truth preference for restriction lists:
          1. m2m FK (brand_ids / category_ids / product_ids) — dereferenced
             via dutchie_*_id on the target model. Canonical once resolution
             pass runs.
          2. raw_*_restriction_ids_raw Char — Dutchie IDs preserved verbatim
             by the sync pipeline. Fallback when m2m hasn't been populated
             yet (e.g. Dutchie sync ran but the product mirror is lagging)."""
        self.ensure_one()
        calc = CALC_METHOD_ID_TO_STRING.get(
            getattr(self, 'calculation_method_id', 0) or 0,
            'PERCENT_OFF',
        )

        def _from_m2m_or_raw(m2m_records, dutchie_field, raw_field):
            # Prefer FKs if present
            if m2m_records:
                ids = [getattr(r, dutchie_field) for r in m2m_records]
                ids = [i for i in ids if i]
                # Coerce numeric Dutchie IDs back to int for mintinvsvc parity
                coerced = []
                for i in ids:
                    try:
                        coerced.append(int(i))
                    except (TypeError, ValueError):
                        coerced.append(i)
                if coerced:
                    return {'ids': coerced, 'isExclusion': False}
            # Fall back to raw string
            return _parse_raw_restriction(raw_field)

        brands = _from_m2m_or_raw(
            self.brand_ids, 'dutchie_brand_id', self.brand_restriction_ids_raw,
        )
        # Exclusions override if inclusions empty
        if not brands and self.exclude_brand_ids:
            ex = [r.dutchie_brand_id for r in self.exclude_brand_ids if r.dutchie_brand_id]
            if ex:
                brands = {'ids': [int(x) if str(x).isdigit() else x for x in ex],
                          'isExclusion': True}

        categories = _from_m2m_or_raw(
            self.category_ids, 'dutchie_category_id', self.category_restriction_ids_raw,
        )
        if not categories and self.exclude_category_ids:
            ex = [r.dutchie_category_id for r in self.exclude_category_ids if r.dutchie_category_id]
            if ex:
                categories = {'ids': [int(x) if str(x).isdigit() else x for x in ex],
                              'isExclusion': True}

        products = _from_m2m_or_raw(
            self.product_ids, 'dutchie_product_id', self.product_restriction_ids_raw,
        )
        if not products and self.exclude_product_ids:
            ex = [r.dutchie_product_id for r in self.exclude_product_ids if r.dutchie_product_id]
            if ex:
                products = {'ids': [int(x) if str(x).isdigit() else x for x in ex],
                            'isExclusion': True}

        # Weight restriction — the "ids" ARE gram floats, mirroring Dutchie's
        # Reward.Restrictions.Weight where RestrictionIds are gram values
        # (3.5 = eighth). mintinvsvc's resolveComputedProductIds already
        # consumes exactly this shape (discountSync.js expandWeightIds,
        # 0.01g tolerance with name/size parse fallback). Inclusion wins over
        # exclusion, same single-bucket precedence as brands/categories above
        # and same as Dutchie's one Weight bucket.
        weights = {}
        incl_w = sorted({
            round(float(w.value), 4)
            for w in self.weight_ids if w.value and w.value > 0
        })
        if incl_w:
            weights = {'ids': incl_w, 'isExclusion': False}
        else:
            excl_w = sorted({
                round(float(w.value), 4)
                for w in self.exclude_weight_ids if w.value and w.value > 0
            })
            if excl_w:
                weights = {'ids': excl_w, 'isExclusion': True}

        # Vendor restriction — res.partner has no Dutchie vendor id, so emit
        # Odoo partner ids plus names; mintinvsvc inventory rows carry
        # vendor_name/vendor_id, so name is the viable join key when the
        # resolver grows vendor support. Serialized now so the data stops
        # dying at the Odoo boundary.
        vendors = {}
        vend, vend_excl = self.vendor_ids, False
        if not vend and self.exclude_vendor_ids:
            vend, vend_excl = self.exclude_vendor_ids, True
        if vend:
            vendors = {
                'ids': vend.ids,
                'names': [v.name for v in vend if v.name],
                'isExclusion': vend_excl,
            }

        # Excluded SKUs — normalized uppercase list (matches the register-side
        # matcher _excluded_sku_set). mintinvsvc already consumes this key:
        # resolveComputedProductIds phase-3 post-filter drops matching SKUs
        # from computed_product_ids (case-insensitive, trim-tolerant).
        excluded_skus = sorted(self._excluded_sku_set()) or None

        # Determine the numeric discount_id. Prefer the Dutchie one if parsable;
        # fall back to an Odoo-synthetic (id + 1M) to stay distinct from
        # Dutchie's numeric range (which tops out in the 400k range today).
        dsid_raw = self.dutchie_discount_id or ''
        try:
            discount_id = int(dsid_raw)
        except ValueError:
            discount_id = self.id + 1_000_000

        return {
            # Identity
            'id': f"{location_uuid}_{self.source or 'odoo'}_{discount_id}",
            'source': self.source or 'odoo',
            'source_external_id': dsid_raw or str(self.id),
            'dutchie_discount_id': dsid_raw or None,
            'discount_id': discount_id,
            'discount_name': self.name,
            'discount_code': self.code or None,
            # Reward. `discount_amount` is what mintinvsvc/server.js reads to
            # compute the price, but ids 1 (FLAT_AMOUNT_OFF) / 5 (DOLLAR_OFF_TOTAL)
            # / 15 (LOYALTY) carry their numeric value in discount_value (their
            # discount_amount is 0). discount_value_for() reads whichever field
            # holds the value, so those deals stop emitting 0 and start applying.
            # `discount_percent` was orphaned (always 0.0, never consumed) — dropped.
            'discount_type': self.discount_type or None,
            'discount_amount': discount_value_for(self),
            'calculation_method': calc,
            'discount_value': getattr(self, 'discount_value', 0.0) or 0.0,
            # Status
            'is_active': self.is_active,
            'is_published': self.is_published,
            'is_available_online': self.is_available_online,
            'first_time_customer_only': self.first_time_customer_only,
            'can_stack_automatically': bool(getattr(self, 'can_stack_automatically', False)),
            'apply_to_only_one_item': bool(getattr(self, 'apply_to_only_one_item', False)),
            # Validity
            'valid_from': self.valid_from.isoformat() if self.valid_from else None,
            'valid_until': self.valid_until.isoformat() if self.valid_until else None,
            'start_time': self.start_time or 0,
            'end_time': self.end_time or 0,
            'monday': self.monday, 'tuesday': self.tuesday, 'wednesday': self.wednesday,
            'thursday': self.thursday, 'friday': self.friday, 'saturday': self.saturday,
            'sunday': self.sunday,
            # Thresholds
            'threshold_type': self.threshold_type or 'none',
            'threshold_min': self.threshold_min or 0,
            'threshold_max': self.threshold_max or 0,
            'minimum_items_required': self.minimum_items_required or 1,
            # Targeting
            'brands': brands,
            'product_categories': categories,
            'products': products,
            'weights': weights,
            'vendors': vendors,
            'excluded_skus': excluded_skus,
            # Display
            'menu_display_name': self.menu_display_name or None,
            'menu_display_image_url': self.menu_display_image_url or None,
            'online_name': getattr(self, 'online_name', None) or None,
            'deal_classification': self.deal_classification or 'sale',
            'sales_details': self.description or None,
        }

    # ── Admin actions ────────────────────────────────────────────────

    def action_push_to_redis(self):
        """Admin-button force push. Enqueues linked stores immediately."""
        self._enqueue_redis_push(changed_fields=None)
        self.env['mint.redis.push.queue'].sudo()._cron_process()
        return True

    @api.model
    def action_push_all_stores_to_redis(self):
        """Enqueue full resync for every dispensary. Drain within 30s."""
        self.env['mint.redis.push.queue'].sudo()._cron_full_resync_discounts()
        return True
