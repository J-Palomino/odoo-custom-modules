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
    application_method_id_for,
    calc_method_id_for,
    discount_value_for,
    parse_raw_restriction,
    redemption_fields_for,
)

from .deal_mixins import coerce_dutchie_ids

_logger = logging.getLogger(__name__)

# System-parameter knobs
PUSH_MODE_PARAM = 'mint.dutchie_discount_push.mode'   # off | dry-run | live
PUSH_URL_PARAM = 'mint.dutchie_discount_push.url'     # mintinvsvc base URL
DEFAULT_PUSH_URL = 'https://mintinvsvc-production-6aa5.up.railway.app/api/admin/discounts'
PUSH_API_KEY_PARAM = 'mint.dutchie_discount_push.api_key'  # mirrors mint.ptl_sync.api_key

# Backoffice review-link template. Tokens: {id} (Dutchie discount id),
# {locId}, {lspId}. Route confirmed from the backoffice SPA bundle
# (marketing.discounts.createNew = '/marketing/discounts/discount/new'; a
# specific discount shares the ':id' sibling route). Templated via system
# parameter so ops can correct the path without a code change.
BACKOFFICE_DISCOUNT_URL_PARAM = 'mint.dutchie_backoffice.discount_url'
DEFAULT_BACKOFFICE_DISCOUNT_URL = (
    'https://themint.backoffice.dutchie.com/marketing/discounts/discount/{id}'
)


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
    dutchie_discount_id = fields.Integer(
        string='Dutchie Discount Id',
        index=True,
        help='The PER-STORE Dutchie discount id returned by a successful live '
             'push. Each store has its own Dutchie discount (distinct id per '
             'loc), so update-vs-create on re-publish is resolved from this '
             'log per (discount, store) — NOT from a single id on mint.discount.',
    )
    backoffice_url = fields.Char(
        string='Backoffice URL',
        help='Deep link to review this discount in the Dutchie backoffice. '
             'Populated on a successful live push (needs the Dutchie discount '
             'id returned by mintinvsvc); empty for dry-run / failed pushes.',
    )


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

    def _resolve_application_method_id(self, discount):
        # Single source of truth in discount_canonical (shared with the
        # submission publish path in dutchie_publish.py).
        return application_method_id_for(discount.application_method)

    def _resolve_redemption_fields(self, discount):
        # Single source of truth in discount_canonical. Verified single-use
        # shape comes from there; see redemption_fields_for().
        return redemption_fields_for(discount.maximum_usage_count)

    def _build_backoffice_url(self, discount_id, loc_id, lsp_id):
        """Build the Dutchie backoffice review URL for a discount.

        Templated via BACKOFFICE_DISCOUNT_URL_PARAM (tokens {id}/{locId}/{lspId})
        so ops can correct the path in one place without a code change.
        """
        tmpl = self.env['ir.config_parameter'].sudo().get_param(
            BACKOFFICE_DISCOUNT_URL_PARAM, DEFAULT_BACKOFFICE_DISCOUNT_URL)
        try:
            return tmpl.format(id=discount_id, locId=loc_id, lspId=lsp_id)
        except Exception:
            # Malformed ops-edited template (stray brace / unknown token):
            # emit nothing rather than storing a broken half-rendered link.
            _logger.warning('Dutchie backoffice URL template invalid: %r', tmpl)
            return ''

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
        # Dutchie's update-discount-item needs an integer Id (0 = create new,
        # a real id = update THAT record). Each store gets its OWN Dutchie
        # discount (distinct id per loc), so the id must be resolved PER
        # (discount, store) — reusing a single id across stores would make
        # store N update store 1's discount. Look it up from the most recent
        # successful live push log for this exact (discount, store); absent →
        # 0 (create) and the live response records the new id on its own log.
        prior = self.env['mint.dutchie.discount.push.log'].sudo().search([
            ('discount_id', '=', discount.id),
            ('company_id', '=', store.id),
            ('mode', '=', 'live'),
            ('success', '=', True),
            ('dutchie_discount_id', '!=', 0),
        ], order='id desc', limit=1)
        existing_int = prior.dutchie_discount_id or 0

        # Loyalty redemption: "this product, free, once". Branch here for the
        # same reason as the welcome coupon — it has no market or plotted days,
        # and the generic PTL path below produces a payload that cannot work:
        #   * no Reward value or scope (the record carries neither, by design —
        #     calculation_method_id on a redemption retypes it to 'percent' and
        #     hides it from /rewards), so Dutchie stored a null-value discount
        #     with NO restrictions: a 100%-off across the ENTIRE CATALOG;
        #   * PlatformTypeRestrictions=[{PlatformTypeId: 2}] ("Online"), which on
        #     an in-store coupon makes it redeemable NOWHERE — the register
        #     answers "this coupon is not available on this origin platform".
        # Both were observed live before this branch existed.
        if discount.discount_type == 'loyalty_redemption':
            return self._redemption_payload(discount, store, existing_int)

        # Welcome free pre-roll (task #102149): a standalone code coupon with a
        # fixed, live-verified reward shape (mirrors PHXNTPR 383481) and a per-LSP
        # pre-roll category. Branch BEFORE the PTL-specific logic below, which
        # reads self.market_id (this method can be called on an empty mint.ptl.day
        # for welcome coupons, which have no market/day).
        if getattr(discount, 'is_welcome_preroll', False):
            cfg = discount._welcome_preroll_config()
            cat = cfg['lsp_categories'].get(self._resolve_lsp_id(store))
            if not cat:
                raise ValueError(
                    'welcome pre-roll: no pre-roll category configured for LSP %s '
                    '(store %s) — refusing to publish an un-scoped free coupon'
                    % (self._resolve_lsp_id(store), store.id))
            return self._welcome_preroll_payload(discount, store, existing_int, cat)

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

        # Day-of-week scoped to THIS push's market (self.market_id) and bounded
        # by the discount's validity span. A deal plotted Mon-in-AZ + Fri-in-MO
        # publishes Mon only to AZ and Fri only to MO — reading the market-blind
        # booleans on mint.discount leaked both weekdays into both markets (#7),
        # and a >60-day-out deal collapsed to all-False = every-day (#6). The
        # all-False guard in _push_one_discount refuses to send an empty set for
        # a day-scoped deal.
        if self.market_id:
            day_bools = discount._compute_weekday_bools(market=self.market_id)
        else:
            # Standalone publish (no PTL day/market) — e.g. a manually-authored
            # code coupon pushed via action_publish_to_dutchie. Use the discount's
            # own day flags; all-False ⇒ every day (Dutchie default).
            day_bools = {d: bool(getattr(discount, d, False)) for d in
                         ('sunday', 'monday', 'tuesday', 'wednesday',
                          'thursday', 'friday', 'saturday')}

        # Code-coupon support: application method + register code + usage cap are
        # read from mint.discount (default to an automatic, code-less, uncapped
        # deal — unchanged for existing PTL deals).
        app_method_id = self._resolve_application_method_id(discount)
        discount_code = discount.dutchie_discount_code or ''
        redemption = self._resolve_redemption_fields(discount)

        return {
            'Id': existing_int,
            'ApplicationMethodId': app_method_id,  # 1=Automatic; 2=Manual; 3=Code
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
            'RedemptionLimit': redemption['RedemptionLimit'],
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
            'DiscountCode': discount_code,
            'MaxRedemptions': redemption['MaxRedemptions'],
            'RedemptionLimitCountingMode': redemption['RedemptionLimitCountingMode'],
            # Day-of-week. mint.discount stores monday..sunday as separate
            # booleans (set by _recompute_day_booleans from the PTL day binding).
            # If all 7 are False, Dutchie treats the discount as every-day.
            'Sunday': day_bools['sunday'],
            'Monday': day_bools['monday'],
            'Tuesday': day_bools['tuesday'],
            'Wednesday': day_bools['wednesday'],
            'Thursday': day_bools['thursday'],
            'Friday': day_bools['friday'],
            'Saturday': day_bools['saturday'],
            'MenuDisplayRank': 0,
            # Customer-facing menu card name. Without DiscountMenuDisplayDetails
            # /MenuDisplayName, Dutchie's online menu falls back to
            # DiscountDescription (internal "lgm ..." text) and mintinvsvc warns
            # "SOP §7: MenuDisplayName is missing". Mirror OnlineName so the
            # storefront card shows the deal name. Shape per the canonical
            # fixture / scripts/dutchie/publish-deal.mjs.
            'DiscountMenuDisplayDetails': {
                'DiscountId': existing_int,
                'MenuDisplayImageUrl': '',
                'MenuDisplayName': name,
                'MenuDisplayDescription': '',
                'DiscountMenuDisplayId': None,
            },
        }

    def _redemption_payload(self, discount, store, existing_int):
        """Dutchie payload for a loyalty redemption — one product, 100% off, once.

        Mirrors the live-verified welcome-coupon shape (the only coupon shape
        proven to work at these registers) and swaps its category/weight scope
        for a single-product scope taken from the redeemed product.

        Reward shape is expressed HERE rather than on mint.discount on purpose:
        writing calculation_method_id onto the record makes
        mint_dutchie_discount_mirror derive discount_type='percent', which drops
        the coupon out of every /rewards and consume query.

        Returns None when the product carries no Dutchie id — the caller must
        not push an unscoped coupon, since with empty restrictions a 100%-off
        applies to the whole catalog.
        """
        product = discount.product_ids[:1] or discount.redemption_product_id
        dutchie_pid = getattr(product, 'dutchie_product_id', None)
        if not dutchie_pid:
            _logger.error(
                'Redemption %s: product %s has no dutchie_product_id — refusing '
                'to build an unscoped payload (would discount the entire catalog)',
                discount.redemption_code, product.display_name if product else '-')
            return None

        empty = lambda: {'IsExclusion': False, 'RestrictionIds': []}
        restrictions = {k: empty() for k in (
            'Strain', 'Weight', 'Category', 'Tag', 'InventoryTag',
            'Tier', 'Brand', 'Vendor', 'Product')}
        restrictions['Product'] = {
            'IsExclusion': False, 'RestrictionIds': [int(dutchie_pid)],
        }

        pid = discount.redemption_partner_id.id or discount.id
        label = (product.name or 'Reward')[:60]
        # One-time by construction: a redemption is a single earned coupon.
        max_uses = int(discount.maximum_usage_count or 1) or 1
        return {
            'Id': existing_int,
            # 3 = Code: the budtender types the code the customer shows.
            'ApplicationMethodId': 3,
            'CanStackAutomatically': False, 'Constraints': [],
            # The code the customer sees IS the Dutchie code (create_redemption
            # writes redemption_code to both fields).
            'DiscountCode': discount.dutchie_discount_code or discount.redemption_code or '',
            'DiscountDescription': f'lgm | loyalty redemption (partner {pid}, discount {discount.id})',
            'ExternalId': f'lgm_redemption_{discount.id}',
            'FirstTimeCustomerOnly': 0, 'IgnoreNetTax': False,
            'IsAvailableOnline': False, 'IsBundledDiscount': False,
            'LocationRestrictions': [], 'OnlineName': label,
            'PaymentRestrictions': {'PayByBankSignupIncentive': False},
            'RequireManagerApproval': False,
            'RestrictToGroupIds': [], 'RestrictToSegmentIds': [],
            # MUST stay empty — see the branch comment. PlatformTypeId 2 is
            # "Online"; on an in-store coupon it makes the discount redeemable
            # nowhere.
            'PlatformTypeRestrictions': [],
            'OrderTypeRestrictions': [],
            'Reward': {
                'DiscountRewardId': None, 'HasThreshold': True,
                'ApplyToOnlyOneItem': False,
                # 2 = PERCENT_OFF with DiscountValue 1 => 100% off, matching the
                # welcome coupon that works at these registers.
                'CalculationMethodId': 2, 'DiscountValue': 1,
                'IncludeNonCannabis': True, 'ItemGroupTypeId': 5,
                'ManualDefaultApplyTo': 1, 'Restrictions': restrictions,
                # Exactly one item.
                'ThresholdMax': 1, 'ThresholdMin': 1, 'ThresholdTypeId': 1,
            },
            'SavedWithAdvancedOptions': False,
            'ValidDateFrom': self._format_dutchie_date(discount.valid_from),
            'ValidDateTo': self._format_dutchie_date(discount.valid_until),
            # MaxRedemptions / RedemptionLimit / counting mode come from the
            # canonical registry so the single-use cap shape stays in one place.
            **redemption_fields_for(max_uses),
            # All-None = every day. A redemption is not day-scoped; sending all
            # seven False would read to Dutchie as "every day" anyway, but None
            # matches the working coupon payload exactly.
            'Sunday': None, 'Monday': None, 'Tuesday': None, 'Wednesday': None,
            'Thursday': None, 'Friday': None, 'Saturday': None,
            'MenuDisplayRank': 0,
            'DiscountMenuDisplayDetails': {
                'DiscountId': existing_int, 'MenuDisplayImageUrl': '',
                'MenuDisplayName': label,
                'MenuDisplayDescription': '', 'DiscountMenuDisplayId': None,
            },
        }

    def _welcome_preroll_payload(self, discount, store, existing_int, category_id):
        """Dutchie payload for a welcome free pre-roll (task #102149).

        Fixed, live-verified shape from record PHXNTPR (Dutchie Id 383481):
        ApplicationMethodId=3 (Code) + DiscountCode, 100%-off exactly one item
        (percent value 1.0 + 1-item threshold), pre-roll Category (per-LSP) +
        0.7g Weight, single-use (MaxRedemptions), in-store only. Day booleans
        left None ⇒ every day. ExternalId keyed to the partner for idempotent
        re-publish.
        """
        empty = lambda: {'IsExclusion': False, 'RestrictionIds': []}
        restrictions = {k: empty() for k in (
            'Strain', 'Weight', 'Category', 'Tag', 'InventoryTag',
            'Tier', 'Brand', 'Vendor', 'Product')}
        restrictions['Category'] = {'IsExclusion': False, 'RestrictionIds': [int(category_id)]}
        restrictions['Weight'] = {'IsExclusion': False, 'RestrictionIds': [0.7]}
        pid = discount.redemption_partner_id.id or discount.id
        max_uses = int(discount.maximum_usage_count or 1) or 1
        return {
            'Id': existing_int, 'ApplicationMethodId': 3,
            'CanStackAutomatically': False, 'Constraints': [],
            'DiscountCode': discount.dutchie_discount_code or '',
            'DiscountDescription': f'lgm | welcome free pre-roll (partner {pid})',
            'ExternalId': f'lgm_welcome_{pid}',
            'FirstTimeCustomerOnly': 0, 'IgnoreNetTax': False,
            'IsAvailableOnline': False, 'IsBundledDiscount': False,
            'LocationRestrictions': [], 'OnlineName': 'Welcome Free Pre-Roll',
            'PaymentRestrictions': {'PayByBankSignupIncentive': False},
            'RedemptionLimit': None, 'RequireManagerApproval': False,
            'RestrictToGroupIds': [], 'RestrictToSegmentIds': [],
            # MUST stay empty. PlatformTypeId 2 is "Online", and this coupon is
            # in-store only (IsAvailableOnline=False) — restricting it to the
            # online platform makes it redeemable nowhere: the register rejects
            # it with "this coupon is not available on this origin platform".
            # Every working Code discount in LSP 575, including the PHXNTPR
            # pre-roll this payload mirrors, carries no platform restriction.
            'PlatformTypeRestrictions': [],
            'OrderTypeRestrictions': [],
            'Reward': {
                'DiscountRewardId': None, 'HasThreshold': True, 'ApplyToOnlyOneItem': False,
                'CalculationMethodId': 2, 'DiscountValue': 1, 'IncludeNonCannabis': True,
                'ItemGroupTypeId': 5, 'ManualDefaultApplyTo': 1, 'Restrictions': restrictions,
                'ThresholdMax': 1, 'ThresholdMin': 1, 'ThresholdTypeId': 1,
            },
            'SavedWithAdvancedOptions': False,
            'ValidDateFrom': self._format_dutchie_date(discount.valid_from),
            'ValidDateTo': self._format_dutchie_date(discount.valid_until),
            'MaxRedemptions': max_uses, 'RedemptionLimitCountingMode': 0,
            'Sunday': None, 'Monday': None, 'Tuesday': None, 'Wednesday': None,
            'Thursday': None, 'Friday': None, 'Saturday': None,
            'MenuDisplayRank': 0,
            'DiscountMenuDisplayDetails': {
                'DiscountId': existing_int, 'MenuDisplayImageUrl': '',
                'MenuDisplayName': 'Welcome Free Pre-Roll',
                'MenuDisplayDescription': '', 'DiscountMenuDisplayId': None,
            },
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
            # Cross-path mutex (#2 stopgap): if this deal's live discount is
            # already owned by the submission/convert path, skip the PTL live
            # push to avoid a duplicate record. First live writer wins; same-path
            # re-publish is allowed. Dry-run never claims/blocks (no live write).
            if mode == 'live' and discount.ptl_deal_id and \
                    not discount.ptl_deal_id._dutchie_claim('ptl'):
                _logger.info(
                    "Dutchie push: discount %s skipped — deal already published "
                    "via submission path (dutchie_publish_owner='submission')",
                    discount.id)
                Log.create({
                    'discount_id': discount.id,
                    'company_id': enabled_stores[:1].id,
                    'mode': mode,
                    'success': False,
                    'error_message': (
                        "Skipped: deal already published to Dutchie via the "
                        "submission/convert path (#2 mutex). Republish from the "
                        "submission, or clear dutchie_publish_owner to switch."),
                })
                continue
            # Honor per-discount store filter if set
            target_stores = discount.store_ids & enabled_stores if discount.store_ids \
                            else enabled_stores
            for store in target_stores:
                self._push_one_discount(discount, store, mode, url, api_key, Log)

    # ─── Deactivate (expire / revoke) — the inverse of the push ──────────

    def _deactivate_discounts_in_dutchie(self, discounts):
        """Pull discounts down from Dutchie by re-POSTing them with IsDeleted=True.

        The inverse of _push_discounts_to_dutchie, called when a deal is expired
        or revoked in Odoo. Expiring in Odoo alone never stopped the Dutchie
        discount — it kept running until its own ValidDateTo — because the
        publish was one-directional. This closes that gap for the day/discount
        push path (mint.dutchie_discount_push.mode).

        The authoritative "where is this live in Dutchie" set is the push log:
        every (discount, store) row with mode='live', success, and a real
        dutchie_discount_id. Dry-run / never-pushed discounts have no such row,
        so they no-op. Gated by the same mode param as publish (off → no-op;
        dry-run → log only; live → POST IsDeleted), so a store can never be
        deactivated by a path that couldn't publish it.

        On a fully-successful live deactivation (no failed stores) we set
        mint.discount.dutchie_is_deleted so Odoo's own state matches Dutchie
        immediately, before the next inbound discount sync re-confirms it.
        """
        if not discounts:
            return
        mode = self._get_dutchie_push_mode()
        if mode == 'off':
            return  # symmetric with publish — nothing was ever sent live

        url = self._get_dutchie_push_url()
        api_key = self._get_dutchie_push_api_key()
        Log = self.env['mint.dutchie.discount.push.log'].sudo()
        Company = self.env['res.company'].sudo()

        for discount in discounts:
            live_logs = Log.search([
                ('discount_id', '=', discount.id),
                ('mode', '=', 'live'),
                ('success', '=', True),
                ('dutchie_discount_id', '!=', 0),
            ])
            stores = Company.browse(sorted(set(live_logs.mapped('company_id').ids)))
            if not stores:
                _logger.info(
                    'Dutchie deactivate: discount %s has no live push — nothing to pull',
                    discount.id)
                continue
            results = [
                self._push_one_discount(discount, store, mode, url, api_key, Log, is_delete=True)
                for store in stores
            ]
            if mode == 'live' and results and all(results):
                try:
                    discount.sudo().write({'dutchie_is_deleted': True})
                except Exception as e:  # noqa: BLE001 — best-effort state mirror
                    _logger.warning(
                        'Dutchie deactivate: could not set dutchie_is_deleted on %s: %s',
                        discount.id, e)

    # ─── Restriction resolvers (Reward.Restrictions.*) ─────────────────────

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
            ids = coerce_dutchie_ids(discount.brand_ids, 'dutchie_brand_id')
            if ids:
                return {'IsExclusion': False, 'RestrictionIds': ids}
        if discount.exclude_brand_ids:
            ids = coerce_dutchie_ids(discount.exclude_brand_ids, 'dutchie_brand_id')
            if ids:
                return {'IsExclusion': True, 'RestrictionIds': ids}
        return self._restriction_from_raw(discount.brand_restriction_ids_raw)

    def _resolve_category_restriction(self, discount):
        """{IsExclusion, RestrictionIds} for Reward.Restrictions.Category.

        Same positive-wins precedence as brand. Uses product.category records
        already resolved by _deal_to_discount_vals' master-cat expansion.
        """
        if discount.category_ids:
            ids = coerce_dutchie_ids(discount.category_ids, 'dutchie_category_id')
            if ids:
                return {'IsExclusion': False, 'RestrictionIds': ids}
        if discount.exclude_category_ids:
            ids = coerce_dutchie_ids(discount.exclude_category_ids, 'dutchie_category_id')
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
            ids = coerce_dutchie_ids(discount.product_ids, 'dutchie_product_id')
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
                ids = coerce_dutchie_ids(tmpls, 'dutchie_product_id')
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

    def _push_one_discount(self, discount, store, mode, url, api_key, Log, is_delete=False):
        """Build payload, log, and (in 'live' mode only) POST to mintinvsvc.

        With is_delete=True this is the EXPIRE/REVOKE mirror: it re-POSTs the
        discount with IsDeleted=True so Dutchie stops applying it. Only an
        EXISTING Dutchie discount can be deleted, so the payload's Id (resolved
        per-store from the prior live push log by _deal_to_dutchie_payload) must
        be non-zero — Id=0 means we never pushed it live here, so there's
        nothing to pull and we no-op. Returns True on success (dry-run and the
        nothing-to-delete case count as success); False on a config gap or a
        failed HTTP call.
        """
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
            return False

        # Unbuildable payload guard: the builders return None when a required
        # scope is missing (a redemption product with no dutchie_product_id, a
        # welcome coupon with no per-LSP pre-roll category). POSTing that sends
        # 'discount': null, and an accepted-but-unscoped coupon is worse than no
        # coupon: with empty Restrictions, Dutchie applies the reward to the
        # ENTIRE CATALOG. Fail closed and log which store needs the backfill.
        if payload['discount'] is None:
            Log.create({
                'discount_id': discount.id,
                'company_id': store.id,
                'dutchie_loc_id': str(store.dutchie_store_id or ''),
                'mode': mode,
                'request_payload': json.dumps(payload, default=str)[:8000],
                'success': False,
                'error_message': (
                    'Skipped: could not build a scoped payload for this discount '
                    'at this store (missing product/category Dutchie id). '
                    'Refusing to push an unscoped reward.'
                ),
            })
            return False

        # All-False day guard (#6): Dutchie reads all-7-days-False as "active
        # EVERY day". A day-scoped deal (one with plotted days) that resolves to
        # NO weekday in this market/span is a bug, not an every-day deal — refuse
        # the write rather than silently running it daily. Continuous deals with
        # no plotted days legitimately use all-False and are exempt.
        disc = payload['discount']
        deal = discount.ptl_deal_id
        if (not is_delete and deal and deal.day_ids and not any(
                disc.get(d) for d in ('Sunday', 'Monday', 'Tuesday', 'Wednesday',
                                      'Thursday', 'Friday', 'Saturday'))):
            Log.create({
                'discount_id': discount.id,
                'company_id': store.id,
                'dutchie_loc_id': str(store.dutchie_store_id or ''),
                'mode': mode,
                'request_payload': json.dumps(payload, default=str)[:8000],
                'success': False,
                'error_message': (
                    'Skipped: day-scoped deal resolved to NO weekday for market '
                    '%s within [%s, %s]. Sending all-False would make Dutchie run '
                    'this EVERY day. Check the deal is plotted on published days '
                    'inside its validity window.' % (
                        self.market_id.code if self.market_id else '?',
                        discount.valid_from, discount.valid_until)
                ),
            })
            return False

        # Resurrection guard (#9): re-publishing (non-delete) a discount that was
        # deactivated in Dutchie reuses its id and POSTs without IsDeleted,
        # reviving it. Allow that ONLY when the deal is genuinely active again
        # (re-approved / live); otherwise a stray push would silently un-delete
        # an expired/revoked deal. The legitimate reactivation path clears the
        # flag on its successful publish below.
        if (not is_delete and mode == 'live' and discount.dutchie_is_deleted
                and not (discount.ptl_deal_id
                         and discount.ptl_deal_id.state in ('approved', 'live'))):
            Log.create({
                'discount_id': discount.id,
                'company_id': store.id,
                'dutchie_loc_id': str(store.dutchie_store_id or ''),
                'mode': mode,
                'request_payload': json.dumps(payload, default=str)[:8000],
                'success': False,
                'error_message': (
                    "Skipped: discount is deactivated in Dutchie "
                    "(dutchie_is_deleted) and its deal is not active — refusing to "
                    "resurrect it. Re-approve the deal to republish, or clear "
                    "dutchie_is_deleted manually."),
            })
            _logger.info("Dutchie push: discount %s skipped — resurrection guard "
                         "(deactivated + deal not active)", discount.id)
            return False

        if is_delete:
            # Mark the existing Dutchie discount deleted. Id=0 ⇒ never pushed
            # live to this store ⇒ nothing to deactivate (no-op success).
            if not payload['discount'].get('Id'):
                _logger.info(
                    'Dutchie deactivate: no live id for discount %s @ %s — skip',
                    discount.id, store.name)
                return True
            payload['discount']['IsDeleted'] = True
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
            _logger.info('[dry-run] Dutchie %s %s for %s',
                         'deactivate' if is_delete else 'push', discount.id, store.name)
            return True

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
                    # mintinvsvc success shape is {ok, discount_id, ...}; the
                    # raw-fault shape nests the id under dutchie_raw.Data. Read
                    # both so the id is captured on success (the old code only
                    # checked dutchie_raw.Data, which success responses omit).
                    new_id = parsed.get('discount_id') or (parsed.get('dutchie_raw') or {}).get('Data')
                    if isinstance(new_id, int) and new_id > 0:
                        # Record the Dutchie id on THIS (discount, store) log row.
                        # _deal_to_dutchie_payload reads it back per-store to do
                        # update-vs-create on re-publish. Deliberately NOT cached
                        # onto mint.discount.dutchie_discount_id — that single
                        # field can't represent N per-store ids and previously
                        # made store N target store 1's discount.
                        log_vals['dutchie_discount_id'] = new_id
                        # Backoffice review link for this push (live id only).
                        log_vals['backoffice_url'] = self._build_backoffice_url(
                            new_id, loc_id, lsp_id)
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
        # #9: a successful live (re)publish reactivates the discount in Dutchie,
        # so clear the stale deleted flag. The deactivate path sets it True but
        # nothing cleared it, so Odoo's state drifted from Dutchie's.
        if not is_delete and log_vals.get('success') and discount.dutchie_is_deleted:
            try:
                discount.sudo().write({'dutchie_is_deleted': False})
            except Exception:
                _logger.warning('Could not clear dutchie_is_deleted on discount %s',
                                discount.id)
        # Structured audit line — ships to Grafana Loki via mint_loki_logger.
        # The Log table above is the queryable Odoo audit; this line is the
        # cross-service one (same event name as mintinvsvc dealAudit).
        verb = 'ptl_deactivate' if is_delete else 'ptl_push'
        _logger.info("deal.audit %s", json.dumps({
            'event': 'deal.audit',
            'action': verb if log_vals.get('success') else f'{verb}_failed',
            'discount_odoo_id': discount.id,
            'discount_name': discount.name,
            'dutchie_discount_id': log_vals.get('dutchie_discount_id') or None,
            'store': store.name,
            'loc_id': loc_id,
            'lsp_id': lsp_id,
            'mode': mode,
            'elapsed_ms': log_vals.get('elapsed_ms'),
            'error': log_vals.get('error_message'),
            'user_id': self.env.uid,
        }, default=str))
        return bool(log_vals.get('success'))
