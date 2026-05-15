# -*- coding: utf-8 -*-
"""Publish mint.discount records to Dutchie POS via the Backoffice API.

Flow:
    Odoo user saves a `mint.discount` with source='manual' →
    auto-publish gate decides if it's publishable →
    for each company in store_ids with a Dutchie LocId, ensure a
    `mint.discount.dutchie.link` row exists →
    background thread POSTs to mintinvsvc `/api/admin/discounts` per link →
    response writes back to the link (Dutchie Id, state, raw_response).

Manual button on the form calls `action_publish_to_dutchie` synchronously.
A cron retries `state='failed'` and `state='pending'` rows every 15 minutes.

Notes:
- We never fire publish for source='ptl' or source='dutchie'. PTL has its own
  webhook (mint_command_center.ptl_day._push_discounts_to_redis) for the cache
  layer; Dutchie-mirrored records originated in Dutchie and shouldn't loop back.
- Auto-publish is gated on the "publishable" predicate (name + dates +
  reward + at least one mapped store). Drafts with missing fields save fine
  but don't fire a Dutchie write.
- The HTTP call is fire-and-forget from the user's perspective; failures land
  on the link record so they can be inspected and retried.
"""
import json
import logging
import urllib.request
from datetime import date, datetime, time

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Config parameter keys (reuse the existing one for API key; add one for URL).
INVSVC_BASE_URL_PARAM = 'mint.inventory_service.base_url'
INVSVC_API_KEY_PARAM = 'mint.inventory_service.api_key'
DEFAULT_INVSVC_BASE_URL = 'https://mintinvsvc-production-6aa5.up.railway.app'

# Dutchie enum maps — keep alongside docs/discounts.md §7.
APPLICATION_METHOD_ID = {
    'automatic': 1,
    'manual': 2,
    'code': 3,
}

THRESHOLD_TYPE_ID = {
    'none': 0,
    'items': 1,
    'order_total': 2,
    # Add more as the threshold_type selection grows.
}

# Fields whose change should re-publish to Dutchie. Touching any of these on
# a published record sends an update; touching nothing else is a no-op.
PUBLISH_TRIGGER_FIELDS = frozenset([
    'name', 'online_name', 'description', 'code',
    'discount_type', 'discount_amount', 'discount_percent', 'discount_value',
    'application_method', 'calculation_method_id',
    'threshold_type', 'threshold_min', 'threshold_max',
    'item_group_type_id',
    'valid_from', 'valid_until', 'start_time', 'end_time',
    'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday',
    'max_redemptions', 'redemption_limit',
    'is_available_online', 'require_manager_approval', 'can_stack_automatically',
    'first_time_customer_only', 'include_non_cannabis',
    'apply_to_only_one_item', 'highest_or_lowest',
    'brand_ids', 'category_ids', 'product_ids', 'vendor_ids', 'weight_ids',
    'exclude_brand_ids', 'exclude_category_ids', 'exclude_product_ids',
    'exclude_vendor_ids', 'exclude_weight_ids',
    'menu_display_name', 'menu_display_image_url', 'menu_display_rank',
    'store_ids',
])


def _date_to_dutchie(d):
    """Dutchie's update-discount-item accepts 'M/D/YYYY, h:mm:ss AM/PM'.

    The fixture shows '5/9/2026, 4:05:00 PM'. We coerce date → midnight,
    datetime → as-is. Returns None for falsy input."""
    if not d:
        return None
    if isinstance(d, date) and not isinstance(d, datetime):
        d = datetime.combine(d, time.min)
    return d.strftime('%-m/%-d/%Y, %-I:%M:%S %p')


class MintDiscountPublish(models.Model):
    _inherit = 'mint.discount'

    # ── Per-store publish link (inverse of mint.discount.dutchie.link.discount_id)
    dutchie_link_ids = fields.One2many(
        'mint.discount.dutchie.link', 'discount_id',
        string='Dutchie publish links',
    )

    # ── Aggregate publish state shown on the discount form
    publish_state = fields.Selection([
        ('draft', 'Draft'),
        ('ready', 'Ready to publish'),
        ('partial', 'Partially published'),
        ('synced', 'Synced to Dutchie'),
        ('failed', 'Failed'),
    ], compute='_compute_publish_state', store=False, string='Publish status')

    publish_state_summary = fields.Char(
        compute='_compute_publish_state', store=False, string='Publish summary')

    @api.depends('dutchie_link_ids.state', 'source', 'name', 'valid_from',
                 'store_ids', 'discount_type')
    def _compute_publish_state(self):
        for rec in self:
            if rec.source != 'manual':
                rec.publish_state = 'draft'
                rec.publish_state_summary = f'source={rec.source or "?"}'
                continue
            links = rec.dutchie_link_ids
            if not links:
                publishable = rec._is_publishable()
                rec.publish_state = 'ready' if publishable else 'draft'
                rec.publish_state_summary = (
                    'ready to publish' if publishable
                    else 'draft — missing required fields or LocIds'
                )
                continue
            states = {l.state for l in links}
            if states == {'synced'}:
                rec.publish_state = 'synced'
            elif 'failed' in states:
                rec.publish_state = 'failed'
            elif 'synced' in states and (states - {'synced'}):
                rec.publish_state = 'partial'
            else:
                rec.publish_state = 'ready'
            rec.publish_state_summary = ' '.join(
                f'{s}={sum(1 for l in links if l.state == s)}' for s in sorted(states))

    # ── Predicate: is this record publishable to Dutchie? ────────────────
    def _is_publishable(self):
        """Soft check that prevents auto-publishing half-baked drafts.

        Anything that violates SOP-8001 still publishes (Dutchie is the source
        of truth — soft validation matches the existing /api/admin/discounts
        server behavior), but the basics (name + reward + at least one mapped
        store) must be present."""
        self.ensure_one()
        if self.source != 'manual':
            return False
        if not self.name or not self.valid_from or not self.discount_type:
            return False
        # Must have at least one store_id that has a Dutchie LocId set.
        eligible = self.store_ids.filtered('x_dutchie_loc_id')
        if not eligible:
            return False
        return True

    # ── Payload builder ──────────────────────────────────────────────────
    def _to_dutchie_payload(self, store, link=None):
        """Build the Dutchie Discount object for one store.

        Maps mint.discount fields → the shape expected by
        v2/discount/update-discount-item. See docs/discounts.md §7 for the
        ID maps and __tests__/fixtures/discount-381839.json for a real example.

        :param store: res.company (target store; provides LocId/LspId)
        :param link: optional existing mint.discount.dutchie.link — when
                     present and link.dutchie_discount_id is set, we send an
                     UPDATE (Id=existing); otherwise we send a CREATE (Id=0).
        """
        self.ensure_one()
        existing_id = 0
        if link and link.dutchie_discount_id:
            try:
                existing_id = int(link.dutchie_discount_id)
            except (TypeError, ValueError):
                existing_id = 0

        # Brand/category/product/etc. — emit Dutchie-namespace cross-reference
        # IDs only. Records without a cross-reference are dropped (no wrong-
        # namespace IDs leak to Dutchie). Same convention used by the PTL
        # webhook payload in ptl_day._discount_to_webhook_payload.
        def _xref_ids(records, attr):
            out = []
            for r in records:
                v = getattr(r, attr, None)
                if not v:
                    continue
                try:
                    out.append(int(v))
                except (TypeError, ValueError):
                    pass  # mint.brand.dutchie_brand_id is a string — keep numeric only
            return out

        restrictions_block = {
            'Strain':       {'IsExclusion': False, 'RestrictionIds': []},
            'Weight':       {'IsExclusion': False, 'RestrictionIds': _xref_ids(self.weight_ids, 'dutchie_weight_id') or []},
            'Category':     {'IsExclusion': False, 'RestrictionIds': _xref_ids(self.category_ids, 'dutchie_category_id') or []},
            'Tag':          {'IsExclusion': False, 'RestrictionIds': []},
            'InventoryTag': {'IsExclusion': False, 'RestrictionIds': []},
            'Tier':         {'IsExclusion': False, 'RestrictionIds': []},
            'Brand':        {'IsExclusion': False, 'RestrictionIds': _xref_ids(self.brand_ids, 'dutchie_brand_id')},
            'Vendor':       {'IsExclusion': False, 'RestrictionIds': []},
            'Product':      {'IsExclusion': False, 'RestrictionIds': _xref_ids(self.product_ids, 'x_dutchie_product_id') or _xref_ids(self.product_ids, 'dutchie_product_id')},
        }

        reward = {
            'DiscountRewardId': None,
            'HasThreshold': bool(self.threshold_min or self.threshold_max),
            'ApplyToOnlyOneItem': bool(self.apply_to_only_one_item),
            'CalculationMethodId': self.calculation_method_id or 0,
            'DiscountValue': self.discount_value or self.discount_amount or 0,
            'IncludeNonCannabis': bool(self.include_non_cannabis),
            'ItemGroupTypeId': self.item_group_type_id or 6,
            'ManualDefaultApplyTo': 1,
            'Restrictions': restrictions_block,
            'ThresholdMax': self.threshold_max or None,
            'ThresholdMin': self.threshold_min or 0,
            'ThresholdTypeId': THRESHOLD_TYPE_ID.get(self.threshold_type or 'none', 0),
        }

        return {
            'Id': existing_id,
            'ApplicationMethodId': APPLICATION_METHOD_ID.get(self.application_method or 'automatic', 1),
            'CanStackAutomatically': bool(self.can_stack_automatically),
            'Constraints': [],
            'DiscountCode': self.code or '',
            'DiscountDescription': self.name or '',
            'ExternalId': self.external_id or f'odoo-mint-discount-{self.id}',
            'FirstTimeCustomerOnly': 1 if self.first_time_customer_only else 0,
            'IgnoreNetTax': False,
            'IsAvailableOnline': bool(self.is_available_online),
            'IsBundledDiscount': bool(self.is_bundled_discount),
            'LocationRestrictions': [],
            'OnlineName': self.online_name or self.name or '',
            'PaymentRestrictions': {'PayByBankSignupIncentive': False},
            'PlatformTypeRestrictions': [{'PlatformTypeId': 2, 'IsExclusion': False}],
            'OrderTypeRestrictions': [],
            'RedemptionLimit': self.redemption_limit or '',
            'RequireManagerApproval': bool(self.require_manager_approval),
            'RestrictToGroupIds': [],
            'RestrictToSegmentIds': [],
            'Reward': reward,
            'SavedWithAdvancedOptions': False,
            'ValidDateFrom': _date_to_dutchie(self.valid_from),
            'ValidDateTo': _date_to_dutchie(self.valid_until),
            'MaxRedemptions': self.max_redemptions or 0,
            'RedemptionLimitCountingMode': self.redemption_limit_counting_mode or 0,
            # Day-of-week
            'Monday': bool(self.monday),
            'Tuesday': bool(self.tuesday),
            'Wednesday': bool(self.wednesday),
            'Thursday': bool(self.thursday),
            'Friday': bool(self.friday),
            'Saturday': bool(self.saturday),
            'Sunday': bool(self.sunday),
            # Menu display
            'DiscountMenuDisplayDetails': {
                'MenuDisplayName': self.menu_display_name or self.name or '',
                'MenuDisplayImageUrl': self.menu_display_image_url or '',
            },
        }

    # ── Manual button: publish to Dutchie synchronously ──────────────────
    def action_publish_to_dutchie(self):
        """Publish each record to Dutchie via mintinvsvc proxy. Returns a
        sticky notification summarising per-store sync results."""
        synced = 0
        failed = 0
        for rec in self:
            rec._publish_to_dutchie_sync()
            for link in rec.dutchie_link_ids:
                if link.state == 'synced':
                    synced += 1
                elif link.state == 'failed':
                    failed += 1
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Dutchie publish'),
                'message': _('%(synced)s synced, %(failed)s failed') % {
                    'synced': synced, 'failed': failed,
                },
                'type': 'success' if not failed else 'warning',
                'sticky': bool(failed),
            },
        }

    def _publish_to_dutchie_sync(self):
        """Synchronously POST to /api/admin/discounts for each unsynced store.

        Idempotent: synced links are left alone unless force=True in context.
        Caller is responsible for committing the transaction."""
        self.ensure_one()
        if not self._is_publishable():
            raise UserError(_(
                'Discount "%s" is not publishable yet — needs a name, '
                'valid_from, discount_type, and at least one store with a '
                'Dutchie LocId.'
            ) % (self.name or '(unnamed)'))

        force = bool(self.env.context.get('force_republish'))
        Link = self.env['mint.discount.dutchie.link'].sudo()

        # Ensure a link exists for each eligible store_id
        eligible = self.store_ids.filtered('x_dutchie_loc_id')
        for store in eligible:
            link = Link.search([
                ('discount_id', '=', self.id),
                ('location_id', '=', store.id),
            ], limit=1)
            if not link:
                link = Link.create({
                    'discount_id': self.id,
                    'location_id': store.id,
                })
            if link.state == 'synced' and not force:
                continue
            self._publish_one_link(link, store)

    def _publish_one_link(self, link, store):
        """POST one (discount, store) to mintinvsvc. Updates the link in place."""
        get_param = self.env['ir.config_parameter'].sudo().get_param
        base = get_param(INVSVC_BASE_URL_PARAM, DEFAULT_INVSVC_BASE_URL)
        api_key = get_param(INVSVC_API_KEY_PARAM, '')
        if not api_key:
            link.write({
                'state': 'failed',
                'last_error': f'{INVSVC_API_KEY_PARAM} ir.config_parameter is empty — set it before publishing',
                'attempts': link.attempts + 1,
            })
            _logger.error('Dutchie publish skipped: api key not configured')
            return

        payload = self._to_dutchie_payload(store, link=link)
        body = {
            'locId': store.x_dutchie_loc_id,
            'lspId': store.x_dutchie_lsp_id,
            'discount': payload,
        }
        url = f'{base.rstrip("/")}/api/admin/discounts'
        link.write({
            'state': 'pending',
            'raw_request': json.dumps(body)[:65000],
            'attempts': link.attempts + 1,
        })

        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(body).encode('utf-8'),
                headers={'Content-Type': 'application/json', 'X-API-Key': api_key},
                method='POST',
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                status = resp.status
                raw = resp.read().decode('utf-8', errors='replace')
        except Exception as e:
            link.write({
                'state': 'failed',
                'last_error': f'{type(e).__name__}: {e}',
                'raw_response': None,
            })
            _logger.warning('Dutchie publish failed for discount %s store %s: %s',
                            self.id, store.id, e)
            return

        try:
            data = json.loads(raw)
        except Exception:
            data = None

        # mintinvsvc proxy shape: { ok: true, discount_id: <int>, dutchie_raw: ... }
        # On failure: { ok: false, warnings, dutchie_raw: { Result: false, Message } }
        ok = bool(data and data.get('ok'))
        dutchie_id = (data or {}).get('discount_id')
        if ok and dutchie_id:
            link.write({
                'state': 'synced',
                'dutchie_discount_id': str(dutchie_id),
                'synced_at': fields.Datetime.now(),
                'last_error': False,
                'raw_response': raw[:65000],
            })
            # Mirror the Dutchie ID onto the discount itself for the FIRST
            # store linked (legacy field, single-valued).
            if not self.dutchie_discount_id:
                self.with_context(skip_publish=True).write({
                    'dutchie_discount_id': str(dutchie_id),
                    'synced_at': fields.Datetime.now(),
                })
        else:
            msg = (data or {}).get('error') or (data or {}).get('message') or f'HTTP {status}'
            link.write({
                'state': 'failed',
                'last_error': msg[:2000],
                'raw_response': raw[:65000],
            })

    # ── Auto-publish on create / write ───────────────────────────────────
    # Strategy: we DON'T fire the HTTP call from the save transaction.
    # Doing so risks (a) blocking the user's save on Dutchie latency and
    # (b) a thread-vs-transaction race where the background reader sees
    # stale data. Instead, create/refresh `pending` link records and let
    # the 1-minute cron drain them. The manual "Publish to Dutchie" button
    # still fires synchronously for users who want immediate feedback.

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for rec in records:
            if rec.source == 'manual' and rec._is_publishable():
                rec._mark_links_pending()
        return records

    def write(self, vals):
        # Avoid recursion when we mirror dutchie_discount_id back onto self.
        if self.env.context.get('skip_publish'):
            return super().write(vals)

        was_manual = {r.id: r.source == 'manual' for r in self}
        result = super().write(vals)

        touched = set(vals.keys()) & PUBLISH_TRIGGER_FIELDS
        if not touched and 'source' not in vals:
            return result

        for rec in self:
            if rec.source != 'manual':
                continue
            if not rec._is_publishable():
                continue
            # Skip if source just flipped to manual via this write — explicit
            # reclassification, not a publish trigger. Use the button.
            if not was_manual.get(rec.id) and 'source' in vals:
                continue
            rec._mark_links_pending()
        return result

    def _mark_links_pending(self):
        """Ensure a pending link exists for every eligible store_id.

        Existing links keep their state if 'synced' AND no trigger field
        changed since last publish; otherwise they flip back to 'pending'
        for the cron to pick up. Idempotent.
        """
        self.ensure_one()
        Link = self.env['mint.discount.dutchie.link'].sudo()
        eligible = self.store_ids.filtered('x_dutchie_loc_id')
        for store in eligible:
            link = Link.search([
                ('discount_id', '=', self.id),
                ('location_id', '=', store.id),
            ], limit=1)
            if not link:
                Link.create({
                    'discount_id': self.id,
                    'location_id': store.id,
                    'state': 'pending',
                })
            elif link.state != 'pending':
                # Flip synced/failed back to pending — record changed and
                # needs republishing on the next cron tick.
                link.write({'state': 'pending', 'last_error': False})

    # ── Cron: drain pending links and retry failed ones ──────────────────
    @api.model
    def _cron_retry_dutchie_publish(self):
        """Pick up pending + failed links and publish them.

        Caps per-link attempts to prevent hammering Dutchie on
        fundamentally-broken records. Failed links stop after MAX_ATTEMPTS;
        operator can fix the record and hit the manual button to retry.
        """
        MAX_ATTEMPTS = 5
        BATCH_SIZE = 50
        Link = self.env['mint.discount.dutchie.link']
        stale = Link.search([
            ('state', 'in', ('pending', 'failed')),
            ('attempts', '<', MAX_ATTEMPTS),
        ], limit=BATCH_SIZE)
        published = 0
        for link in stale:
            rec = link.discount_id
            if not rec or rec.source != 'manual':
                link.write({'state': 'skipped', 'last_error': 'discount missing or source!=manual'})
                continue
            if not rec._is_publishable():
                link.write({'state': 'skipped', 'last_error': 'discount no longer publishable'})
                continue
            try:
                rec._publish_one_link(link, link.location_id)
                self.env.cr.commit()
                published += 1
            except Exception as e:
                _logger.warning('Dutchie publish cron failed for link %s: %s', link.id, e)
                self.env.cr.rollback()
        _logger.info('Dutchie publish cron: %d/%d processed', published, len(stale))
