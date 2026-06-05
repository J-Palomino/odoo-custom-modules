"""Step 1 of the Odoo → Dutchie discount push wiring.

Pure-additive: introduces fields, helpers, and an audit-log model, but the
mode getter defaults to 'off' so nothing actually hits Dutchie until ops
flips `mint.dutchie_discount_push.mode = live` for an enabled
(market, store) pair.

Lives under mint_command_center because the publish flow that triggers
the push is `mint.ptl.day.action_publish()`. The Dutchie HTTP endpoint
itself (`POST /api/admin/discounts`) is in mintinvsvc and already shipped.

Rollout sequence:
  1. Ship this with mode='off' — zero behavior change.
  2. Flip mode='dry-run' globally; payloads logged to mint.dutchie.discount.push.log.
  3. Pick one canary (market + store) — set both dutchie_discount_push_enabled
     flags to True, mode='live'. Watch the log.
  4. Expand market by market. AZ last (LSP-575 leakage risk per
     dutchie-sandbox-locids-not-isolated.md).
"""
import json
import logging
import threading
import urllib.request

from odoo import api, fields, models
from odoo.addons.mint_api_v2.models.discount_canonical import (
    THRESHOLD_TYPE_ID_BY_ODOO,
    calc_method_id_for,
    discount_value_for,
    parse_raw_restriction,
)

_logger = logging.getLogger(__name__)

# System-parameter knobs
PUSH_MODE_PARAM = 'mint.dutchie_discount_push.mode'   # off | dry-run | live
PUSH_URL_PARAM = 'mint.dutchie_discount_push.url'     # mintinvsvc base URL
DEFAULT_PUSH_URL = 'https://mintinvsvc-production-6aa5.up.railway.app/api/admin/discounts'
PUSH_API_KEY_PARAM = 'mint.dutchie_discount_push.api_key'  # mirrors mint.ptl_sync.api_key


class MintRegionDutchiePush(models.Model):
    _inherit = 'mint.region'

    dutchie_discount_push_enabled = fields.Boolean(
        string='Push Discounts to Dutchie',
        default=False,
        help='Master gate for this market. Even with mode=live, no push '
             'happens unless this flag AND res.company.dutchie_discount_push_enabled '
             'are both True. Per dutchie-sandbox-locids-not-isolated.md, '
             'AZ should be the LAST market enabled.',
    )


class ResCompanyDutchiePush(models.Model):
    _inherit = 'res.company'

    dutchie_discount_push_enabled = fields.Boolean(
        string='Push Discounts to Dutchie',
        default=False,
        help='Per-store opt-in. Both this AND mint.region flag must be True '
             'for the discount push to fire (and global mode must be "live").',
    )
    dutchie_pos_location_id = fields.Integer(
        string='Dutchie POS LocId',
        help='Integer POS LocId Dutchie uses for this store (e.g. Tempe ≈ 1568, '
             'Spring Hill = 2898). Required for the Dutchie discount push — '
             'the existing dutchie_store_id is a UUID and is NOT accepted by '
             'Dutchie /api/v2/discount/update-discount-item, which needs the '
             'integer location id. Mirrors mintinvsvc stores.pos_location_id.',
    )
    dutchie_lsp_id = fields.Integer(
        string='Dutchie LspId',
        help='Integer LSP (Licensed Service Provider) id Dutchie uses to scope '
             'discount/inventory writes. AZ = 575, IL = 805, FL = 821, MI = 822 '
             '(values from packages/inventory-service/db/migrations). Required '
             'for the Dutchie discount push.',
    )


class DutchieDiscountPushLog(models.Model):
    _name = 'mint.dutchie.discount.push.log'
    _description = 'Audit log for Odoo → Dutchie discount push attempts'
    _order = 'create_date desc'
    _rec_name = 'discount_id'

    discount_id = fields.Many2one('mint.discount', string='Discount',
                                  ondelete='set null', index=True)
    company_id = fields.Many2one('res.company', string='Store',
                                 ondelete='set null', index=True)
    dutchie_loc_id = fields.Char(string='Dutchie LocId', index=True)
    mode = fields.Selection(
        selection=[('off', 'Off'), ('dry-run', 'Dry Run'), ('live', 'Live')],
        string='Mode', required=True,
    )
    request_payload = fields.Text(string='Request Payload (JSON)')
    response_body = fields.Text(string='Response (JSON)')
    success = fields.Boolean(string='Success', index=True)
    error_message = fields.Text(string='Error')
    elapsed_ms = fields.Integer(string='Elapsed (ms)')


class PtlDayDutchiePush(models.Model):
    _inherit = 'mint.ptl.day'

    # ─── Mode + URL helpers ──────────────────────────────────────────────

    def _get_dutchie_push_mode(self):
        """Return one of: 'off' (default), 'dry-run', 'live'."""
        get_param = self.env['ir.config_parameter'].sudo().get_param
        mode = (get_param(PUSH_MODE_PARAM, 'off') or 'off').strip().lower()
        if mode not in ('off', 'dry-run', 'live'):
            _logger.warning('Dutchie push: unknown mode %r, treating as off', mode)
            return 'off'
        return mode

    def _get_dutchie_push_url(self):
        get_param = self.env['ir.config_parameter'].sudo().get_param
        return get_param(PUSH_URL_PARAM, DEFAULT_PUSH_URL)

    def _get_dutchie_push_api_key(self):
        get_param = self.env['ir.config_parameter'].sudo().get_param
        # Same key that signs ptl-discount-sync; ops can override per-env.
        return get_param(PUSH_API_KEY_PARAM, get_param('mint.ptl_sync.api_key', ''))

    # ─── Payload translator (Odoo discount → Dutchie discount JSON) ───────

    # ItemGroupTypeId fallback. Per fixture 6 = "Any product matching restrictions
    # within an item-count threshold" (bundle deal). 5 = generic single-item
    # discount. Stored value used if present.
    ITEM_GROUP_TYPE_ID_FALLBACK = 5

    def _resolve_calc_method_id(self, discount):
        # Authoritative: stored calculation_method_id, else map the odoo
        # discount_type via the canonical registry. Default 2 (PERCENT_OFF) —
        # the same safe default the Redis serializer uses; bogo/clearance (not
        # in the registry) fall here, matching the old "treat as percent" intent
        # now that percent is id 2 (it was the inverted id 1 before 2026-06-05).
        return calc_method_id_for(discount) or 2

    def _resolve_item_group_type_id(self, discount):
        if getattr(discount, 'item_group_type_id', 0):
            return int(discount.item_group_type_id)
        return self.ITEM_GROUP_TYPE_ID_FALLBACK

    def _format_dutchie_date(self, dt):
        """Format a date as Dutchie expects ('M/D/YYYY, h:mm:ss A')."""
        if not dt:
            return ''
        # Dutchie API accepts both ISO and the M/D/YYYY format used in the
        # fixture; we use a lossless ISO for stability.
        return f"{dt.month}/{dt.day}/{dt.year}, 12:00:00 AM"

    def _deal_to_dutchie_payload(self, discount, store):
        """Translate (mint.discount, res.company) → Dutchie update-discount-item Discount object.

        Matches the canonical fixture shape at
        __tests__/fixtures/discount-381839.json. Required fields, sensible
        defaults, full Reward block — earlier minimal v1 caused Dutchie 500s
        with "Object reference not set to an instance of an object" because
        the Reward sub-tree was missing.

        v2 leaves all restriction lists empty ([] / RestrictionIds=[]) — SKU /
        brand / category scoping is enforced upstream by the mintinvsvc
        validator and by `excluded_skus` on the PTL deal, not by Dutchie
        itself. Day-of-week recurrence reads the bool flags on mint.discount
        (Monday..Sunday). If all 7 flags are False (the default), Dutchie
        treats the discount as active every day.
        """
        # dutchie_discount_id is a Char (defined in mint_api_v2). For Dutchie's
        # update-discount-item we need an integer Id (0 = create new). PTL-derived
        # rows carry a synthetic 'ptl_<n>' value here that's NOT a Dutchie id;
        # treat those as 0 (create) and let the live response give us the real id.
        existing = (discount.dutchie_discount_id or '').strip()
        existing_int = int(existing) if existing.isdigit() else 0

        name = (discount.name or '')[:120]
        calc_method_id = self._resolve_calc_method_id(discount)
        item_group_type_id = self._resolve_item_group_type_id(discount)
        amount = self._resolve_dutchie_amount(discount)

        # Threshold (BOGO / bundle / "N for $X") — derive from mint.discount
        # threshold_* fields when stored; otherwise infer from discount type.
        threshold_min = float(discount.threshold_min or 0)
        threshold_max = float(discount.threshold_max or 0) if discount.threshold_max else None
        if not threshold_min and discount.discount_type in ('bogo', 'price_to_amount'):
            threshold_min = 2.0
        threshold_type_id = THRESHOLD_TYPE_ID_BY_ODOO.get(
            discount.threshold_type, 0
        ) or (1 if threshold_min else 0)
        has_threshold = bool(threshold_min)

        # Required keys per fixture — Dutchie NREs without them.
        # Brand / Category / Product / Weight are populated by resolver helpers
        # that read already-computed fields on mint.discount (brand_ids etc are
        # set during _deal_to_discount_vals; excluded_skus is forwarded from the
        # PTL deal; weight reads through discount.ptl_deal_id).
        # Strain / Tag / InventoryTag / Tier / Vendor stay empty for v1 —
        # bring those in when ops surfaces them in the PTL deal form (v2).
        empty_restriction = lambda: {'IsExclusion': False, 'RestrictionIds': []}
        reward_restrictions = {
            'Strain': empty_restriction(),
            'Weight':   self._resolve_weight_restriction(discount),
            'Category': self._resolve_category_restriction(discount),
            'Tag': empty_restriction(),
            'InventoryTag': empty_restriction(),
            'Tier': empty_restriction(),
            'Brand':    self._resolve_brand_restriction(discount),
            'Vendor': empty_restriction(),
            'Product':  self._resolve_product_restriction(discount),
        }

        return {
            'Id': existing_int,
            'ApplicationMethodId': 1,  # 1=Automatic; 2=Manual; 3=Code
            'CanStackAutomatically': bool(
                discount.can_stack_automatically
                if discount.can_stack_automatically is not None
                else discount.stack_on_other_discounts
            ),
            'Constraints': [],
            'DiscountDescription': (discount.description or name)[:500],
            # Standing rule (feedback-dutchie-record-prefix-lgm): records we
            # create in Dutchie are prefixed 'lgm', never 'odoo'. This is also
            # the canonical Dutchie<->Odoo join key: lgm_<mint.discount.id>.
            'ExternalId': f'lgm_{discount.id}',
            'FirstTimeCustomerOnly': 1 if discount.first_time_customer_only else 0,
            'IgnoreNetTax': False,
            'IsAvailableOnline': bool(discount.is_available_online),
            'IsBundledDiscount': discount.discount_type in ('bogo',),
            'LocationRestrictions': [],
            # OnlineName is the customer-facing label. Use the same as Name
            # unless ops customizes via mint.discount in the future.
            'OnlineName': name,
            'PaymentRestrictions': {'PayByBankSignupIncentive': False},
            'RedemptionLimit': '',
            'RequireManagerApproval': False,
            'RestrictToGroupIds': [],
            'RestrictToSegmentIds': [],
            # PlatformTypeId 2 = "Online" per the fixture; v1 leaves it as
            # the only platform restriction so the discount applies online.
            'PlatformTypeRestrictions': [{'PlatformTypeId': 2, 'IsExclusion': False}],
            'OrderTypeRestrictions': [],
            'Reward': {
                'DiscountRewardId': None,
                'HasThreshold': has_threshold,
                'ApplyToOnlyOneItem': False,
                'CalculationMethodId': calc_method_id,
                'DiscountValue': amount,
                'IncludeNonCannabis': False,
                'ItemGroupTypeId': item_group_type_id,
                'ManualDefaultApplyTo': 1,
                'Restrictions': reward_restrictions,
                'ThresholdMax': threshold_max,
                'ThresholdMin': threshold_min if threshold_min else None,
                'ThresholdTypeId': threshold_type_id,
            },
            'SavedWithAdvancedOptions': False,
            'ValidDateFrom': self._format_dutchie_date(discount.valid_from),
            'ValidDateTo': self._format_dutchie_date(discount.valid_until),
            'DiscountCode': '',
            'MaxRedemptions': None,
            'RedemptionLimitCountingMode': 0,
            # Day-of-week. mint.discount stores monday..sunday as separate
            # booleans (set by _recompute_day_booleans from the PTL day binding).
            # If all 7 are False, Dutchie treats the discount as every-day.
            'Sunday': bool(discount.sunday),
            'Monday': bool(discount.monday),
            'Tuesday': bool(discount.tuesday),
            'Wednesday': bool(discount.wednesday),
            'Thursday': bool(discount.thursday),
            'Friday': bool(discount.friday),
            'Saturday': bool(discount.saturday),
            'MenuDisplayRank': 0,
        }

    # ─── Push entry point — call AFTER _push_discounts_to_redis ──────────

    def _push_discounts_to_dutchie(self, discount_ids):
        """Step 1 wiring: gated by mode + per-market + per-store flags.

        With mode='off' (default) this no-ops immediately. With 'dry-run'
        it builds the payloads and writes them to mint.dutchie.discount.push.log
        without firing the HTTP call. With 'live' it also POSTs to mintinvsvc.
        """
        if not discount_ids:
            return

        mode = self._get_dutchie_push_mode()
        if mode == 'off':
            return  # ZERO behavior change in this branch

        url = self._get_dutchie_push_url()
        api_key = self._get_dutchie_push_api_key()
        Discount = self.env['mint.discount'].sudo()
        Log = self.env['mint.dutchie.discount.push.log'].sudo()

        discounts = Discount.browse(discount_ids)

        # Per-market gate (this PTL day's market)
        market_enabled = bool(self.market_id and self.market_id.dutchie_discount_push_enabled)
        if not market_enabled:
            _logger.info('Dutchie push: market %s not enabled, skipping %d discount(s)',
                         self.market_id.code if self.market_id else '?', len(discounts))
            return

        # Per-store gate
        store_domain = [
            ('is_dispensary', '=', True),
            ('dutchie_store_id', '!=', False),
            ('region_id', '=', self.market_id.id),
            ('dutchie_discount_push_enabled', '=', True),
        ]
        enabled_stores = self.env['res.company'].sudo().search(store_domain)
        if not enabled_stores:
            _logger.info('Dutchie push: no enabled stores in market %s', self.market_id.code)
            return

        for discount in discounts:
            # Honor per-discount store filter if set
            target_stores = discount.store_ids & enabled_stores if discount.store_ids \
                            else enabled_stores
            for store in target_stores:
                self._push_one_discount(discount, store, mode, url, api_key, Log)

    # ─── Restriction resolvers (Reward.Restrictions.*) ─────────────────────

    @staticmethod
    def _coerce_dutchie_ids(records, field):
        """Coerce Char Dutchie cross-reference IDs on a recordset to ints.
        Lifted from ptl_day.py:_discount_to_webhook_payload. Records with
        empty / non-numeric Dutchie IDs are silently dropped — the downstream
        resolver indexes by Dutchie ID only and would miss them anyway.
        """
        out = []
        for r in records:
            val = getattr(r, field, None)
            if not val:
                continue
            try:
                out.append(int(str(val).strip()))
            except (TypeError, ValueError):
                continue
        return out

    @staticmethod
    def _restriction_from_raw(raw):
        """Fallback to the *_restriction_ids_raw Char when the m2m FK is empty.

        source=dutchie rows store their targeting as raw Dutchie IDs in the
        Char field and leave the m2m FKs empty (only PTL rows populate the
        FKs). Without this the push dropped ALL brand/category/product scoping
        for Dutchie-sourced discounts. Mirrors the Redis serializer's
        _from_m2m_or_raw fallback. Maps canonical {ids,isExclusion} → Dutchie's
        {RestrictionIds,IsExclusion}; non-numeric ids are dropped (Dutchie needs ints).
        """
        parsed = parse_raw_restriction(raw)
        if parsed and parsed['ids']:
            ids = [int(i) for i in parsed['ids'] if str(i).lstrip('-').isdigit()]
            if ids:
                return {'IsExclusion': parsed['isExclusion'], 'RestrictionIds': ids}
        return {'IsExclusion': False, 'RestrictionIds': []}

    def _resolve_brand_restriction(self, discount):
        """{IsExclusion, RestrictionIds} for Reward.Restrictions.Brand.

        Precedence: positive scope (brand_ids) wins. Negative scope
        (exclude_brand_ids) only emitted when no positive scope is set —
        Dutchie's Restriction sub-object can't represent both simultaneously.
        Empty default returned when neither is set.
        """
        if discount.brand_ids:
            ids = self._coerce_dutchie_ids(discount.brand_ids, 'dutchie_brand_id')
            if ids:
                return {'IsExclusion': False, 'RestrictionIds': ids}
        if discount.exclude_brand_ids:
            ids = self._coerce_dutchie_ids(discount.exclude_brand_ids, 'dutchie_brand_id')
            if ids:
                return {'IsExclusion': True, 'RestrictionIds': ids}
        return self._restriction_from_raw(discount.brand_restriction_ids_raw)

    def _resolve_category_restriction(self, discount):
        """{IsExclusion, RestrictionIds} for Reward.Restrictions.Category.

        Same positive-wins precedence as brand. Uses product.category records
        already resolved by _deal_to_discount_vals' master-cat expansion.
        """
        if discount.category_ids:
            ids = self._coerce_dutchie_ids(discount.category_ids, 'dutchie_category_id')
            if ids:
                return {'IsExclusion': False, 'RestrictionIds': ids}
        if discount.exclude_category_ids:
            ids = self._coerce_dutchie_ids(discount.exclude_category_ids, 'dutchie_category_id')
            if ids:
                return {'IsExclusion': True, 'RestrictionIds': ids}
        return self._restriction_from_raw(discount.category_restriction_ids_raw)

    def _resolve_product_restriction(self, discount):
        """{IsExclusion, RestrictionIds} for Reward.Restrictions.Product.

        Two sources combine:
          - discount.product_ids (positive scope — empty for PTL deals today)
          - discount.excluded_skus (text → SKU set → product.template lookup
            by default_code → dutchie_product_id)
        Negative scope (excluded_skus → IsExclusion=True) is only emitted
        when no positive scope exists. If both somehow exist, positive wins
        and the excluded SKUs are dropped from the payload (filtered out
        upstream via the brand+category scope shouldn't include them anyway).
        """
        if discount.product_ids:
            ids = self._coerce_dutchie_ids(discount.product_ids, 'dutchie_product_id')
            if ids:
                return {'IsExclusion': False, 'RestrictionIds': ids}
        # Excluded SKUs path
        if discount.excluded_skus:
            sku_set = discount._excluded_sku_set() if hasattr(discount, '_excluded_sku_set') else None
            if sku_set is None:
                # Fallback parser if the helper isn't on this discount model
                sku_set = {
                    s.strip().lower()
                    for s in discount.excluded_skus.replace(',', '\n').split('\n')
                    if s.strip()
                }
            if sku_set:
                Template = self.env['product.template'].sudo()
                tmpls = Template.search([
                    ('default_code', '!=', False),
                    ('default_code', 'in', list(sku_set) + [s.upper() for s in sku_set]),
                ])
                # Case-insensitive filter (default_code 'in' is case-sensitive in pg)
                tmpls = tmpls.filtered(lambda t: (t.default_code or '').strip().lower() in sku_set)
                ids = self._coerce_dutchie_ids(tmpls, 'dutchie_product_id')
                if ids:
                    return {'IsExclusion': True, 'RestrictionIds': ids}
        return self._restriction_from_raw(discount.product_restriction_ids_raw)

    # Conversion factors to grams (Dutchie Reward.Restrictions.Weight.RestrictionIds
    # expects gram floats, per __tests__/fixtures/discount-381839.json + test at
    # discountSyncTransform.test.js:687 (RestrictionIds: [1.0] = 1 gram).
    WEIGHT_UNIT_TO_GRAMS = {
        'g':  1.0,
        'mg': 0.001,
        'oz': 28.3495,
        # 'ct' = count, NOT a weight — produces no Weight restriction
    }

    def _resolve_weight_restriction(self, discount):
        """{IsExclusion, RestrictionIds:[<gram_float>]} for Reward.Restrictions.Weight.

        Reads weight_value + weight_unit from the linked mint.ptl.deal (the
        weight feature is on ptl.deal, not mint.discount — landed in commit
        bdeb5d1). Returns empty default when unit is 'ct' (count, not weight)
        or fields are unpopulated.
        """
        deal = discount.ptl_deal_id
        if not deal:
            return {'IsExclusion': False, 'RestrictionIds': []}
        value = getattr(deal, 'weight_value', 0) or 0
        unit = getattr(deal, 'weight_unit', '') or ''
        if not value or unit not in self.WEIGHT_UNIT_TO_GRAMS:
            return {'IsExclusion': False, 'RestrictionIds': []}
        grams = float(value) * self.WEIGHT_UNIT_TO_GRAMS[unit]
        # Dutchie expects float grams with reasonable precision (3.5, 7.0, etc.)
        # Round to 4 decimals to avoid 0.99999999 vs 1.0 mismatches with their
        # canonical enum.
        return {'IsExclusion': False, 'RestrictionIds': [round(grams, 4)]}

    def _resolve_dutchie_amount(self, discount):
        """Compute the Amount Dutchie expects for this discount's Reward.

        Reads the value off whichever mint.discount field actually holds it
        (discount_value_for): discount_amount for percent/price types, but
        discount_value for FLAT_AMOUNT_OFF (1) / DOLLAR_OFF_TOTAL (5) /
        LOYALTY (15), whose discount_amount is 0. Keys the percent conversion
        off the canonical CalculationMethodId — NOT the (historically mislabeled)
        discount_type — so a real percent deal converts 0.35 → 35 and a $-off
        deal passes its dollar value through unchanged.
        """
        val = float(discount_value_for(discount) or 0.0)
        if calc_method_id_for(discount) == 2:  # PERCENT_OFF
            # 0.35 → 35 ; 35 → 35 (defensive if someone stored raw percent)
            return val * 100.0 if val <= 1.0 else val
        return val

    def _resolve_pos_loc_id(self, store):
        """Integer POS LocId for this store.

        Prefer the new dutchie_pos_location_id field. Fall back to the
        legacy dutchie_store_id only if it parses as an integer (it's a
        UUID in production, so this fallback returns 0 — which the push
        path treats as a skip signal).
        """
        if getattr(store, 'dutchie_pos_location_id', 0):
            return int(store.dutchie_pos_location_id)
        legacy = store.dutchie_store_id or ''
        return int(legacy) if str(legacy).isdigit() else 0

    def _resolve_lsp_id(self, store):
        """Integer LSP id for this store. v1 reads the per-store override;
        v2 may infer from market_id when the override is unset."""
        return int(getattr(store, 'dutchie_lsp_id', 0) or 0)

    def _push_one_discount(self, discount, store, mode, url, api_key, Log):
        """Build payload, log, and (in 'live' mode only) POST to mintinvsvc."""
        loc_id = self._resolve_pos_loc_id(store)
        lsp_id = self._resolve_lsp_id(store)
        payload = {
            'locId': loc_id,
            'lspId': lsp_id,
            'discount': self._deal_to_dutchie_payload(discount, store),
        }
        # Skip entirely if either id is missing — mintinvsvc rejects locId=0 or
        # lspId=0 with 400, so logging a doomed payload just adds noise. Still
        # write a log row so ops can see which stores need backfill.
        if not loc_id or not lsp_id:
            Log.create({
                'discount_id': discount.id,
                'company_id': store.id,
                'dutchie_loc_id': str(store.dutchie_store_id or ''),
                'mode': mode,
                'request_payload': json.dumps(payload, default=str)[:8000],
                'success': False,
                'error_message': (
                    f'Skipped: store missing dutchie_pos_location_id ({loc_id}) '
                    f'or dutchie_lsp_id ({lsp_id}). Backfill required before '
                    f'this store can push to Dutchie.'
                ),
            })
            return
        log_vals = {
            'discount_id': discount.id,
            'company_id': store.id,
            'dutchie_loc_id': str(store.dutchie_store_id),
            'mode': mode,
            'request_payload': json.dumps(payload, default=str)[:8000],
            'success': False,
        }

        if mode == 'dry-run':
            log_vals['success'] = True
            log_vals['response_body'] = '(dry-run — no HTTP call)'
            Log.create(log_vals)
            _logger.info('[dry-run] Dutchie push %s for %s', discount.id, store.name)
            return

        # mode == 'live': fire the HTTP call (synchronously so we capture the response)
        import time as _t
        t0 = _t.time()
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode('utf-8'),
                headers={
                    'Content-Type': 'application/json',
                    'X-API-Key': api_key,
                    'User-Agent': 'mint-odoo-dutchie-push/1.0',
                },
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = resp.read().decode('utf-8', errors='replace')
                log_vals['elapsed_ms'] = int((_t.time() - t0) * 1000)
                log_vals['response_body'] = body[:8000]
                log_vals['success'] = 200 <= resp.status < 300
                # Cache the returned Dutchie id so subsequent pushes use update-mode
                try:
                    parsed = json.loads(body)
                    new_id = (parsed.get('dutchie_raw') or {}).get('Data')
                    # Field is Char (from mint_api_v2) — store as string. Only
                    # overwrite if currently empty or a synthetic 'ptl_*' marker.
                    cur = (discount.dutchie_discount_id or '').strip()
                    is_synthetic = cur.startswith('ptl_') or not cur
                    if isinstance(new_id, int) and new_id > 0 and is_synthetic:
                        discount.sudo().write({'dutchie_discount_id': str(new_id)})
                except Exception:
                    pass
        except urllib.error.HTTPError as e:
            log_vals['elapsed_ms'] = int((_t.time() - t0) * 1000)
            log_vals['response_body'] = (e.read().decode('utf-8', errors='replace') or '')[:8000]
            log_vals['error_message'] = f'HTTP {e.code}: {e.reason}'
        except Exception as e:
            log_vals['elapsed_ms'] = int((_t.time() - t0) * 1000)
            log_vals['error_message'] = f'{type(e).__name__}: {str(e)[:500]}'

        Log.create(log_vals)
