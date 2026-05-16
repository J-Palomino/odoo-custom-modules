# -*- coding: utf-8 -*-
"""Step 2 of Odoo → Dutchie discount push.

Layered on top of `dutchie_discount_push.py` (step 1, all flags off):

- Adds `res.company.dutchie_loc_id` / `dutchie_lsp_id` (correct Backoffice
  identifiers; step 1 erroneously parsed `dutchie_store_id` as int — that
  field is the consumer-side UUID, so `LocId` always resolved to 0).
- Adds `mint.discount.dutchie.link` for per-store sync state — complements
  step 1's per-attempt `mint.dutchie.discount.push.log`.
- Replaces step 1's stub `_deal_to_dutchie_payload` with a comprehensive
  99-field mapping that mirrors the Dutchie `update-discount-item` shape.
- Surfaces the publish through a manual "Publish to Dutchie" button on the
  `mint.discount` form, AND auto-publishes on save when `source='manual'`
  (gated by step 1's `mint.dutchie_discount_push.mode` config-param — when
  mode='off' the auto-publish is a true no-op, no buffering).
- A 1-minute drain cron picks up pending/failed links so HTTP latency
  never blocks a user's save.

The PTL pipeline (`mint.ptl.day.action_publish`) now also benefits from
the fixed identifiers + comprehensive payload via the override below.
"""
import json
import logging
import urllib.error
import urllib.request
from datetime import date, datetime, time

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Reuse step 1's config-param keys — single source of truth.
from .dutchie_discount_push import (
    PUSH_MODE_PARAM, PUSH_URL_PARAM, PUSH_API_KEY_PARAM, DEFAULT_PUSH_URL,
)

# Dutchie enum maps — keep alongside docs/discounts.md §7.
APPLICATION_METHOD_ID = {'automatic': 1, 'manual': 2, 'code': 3}
THRESHOLD_TYPE_ID = {'none': 0, 'items': 1, 'order_total': 2}

# Fields whose change should re-mark links 'pending'. Touching any of these
# on a `source='manual'` record retriggers the publish path; touching
# anything else is a no-op.
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
    """Dutchie's `update-discount-item` accepts 'M/D/YYYY, h:mm:ss AM/PM'.
    Fixture shows '5/9/2026, 4:05:00 PM'. Date → midnight, datetime → as-is.
    Returns None for falsy input.
    """
    if not d:
        return None
    if isinstance(d, date) and not isinstance(d, datetime):
        d = datetime.combine(d, time.min)
    return d.strftime('%-m/%-d/%Y, %-I:%M:%S %p')


class MintDiscountPublish(models.Model):
    _inherit = 'mint.discount'

    dutchie_link_ids = fields.One2many(
        'mint.discount.dutchie.link', 'discount_id',
        string='Dutchie publish links',
    )

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

    # ── Predicate: is this record publishable? ───────────────────────────
    def _is_publishable(self):
        """Soft check that prevents auto-publishing half-baked drafts.
        Anything that violates SOP-8001 still publishes (Dutchie is the
        source of truth — soft validation matches the existing
        /api/admin/discounts behavior), but the basics (name + reward +
        at least one mapped store) must be present.
        """
        self.ensure_one()
        if self.source != 'manual':
            return False
        if not self.name or not self.valid_from or not self.discount_type:
            return False
        if not self.store_ids.filtered('dutchie_loc_id'):
            return False
        return True

    # ── Payload builder ──────────────────────────────────────────────────
    def _to_dutchie_payload(self, store, link=None):
        """Build the Dutchie Discount object for one store.

        Maps `mint.discount` fields → `update-discount-item` shape.
        See docs/discounts.md §7 for the ID maps and
        __tests__/fixtures/discount-381839.json for a real example.

        Replaces step 1's `mint.ptl.day._deal_to_dutchie_payload`, which
        only handled 5 calc methods, ignored brand/category/product/weight
        restrictions, left WeeklyRecurrenceInfo None, and erroneously
        parsed `dutchie_store_id` (consumer UUID) as `LocId`.

        :param store: res.company target store
        :param link: optional existing mint.discount.dutchie.link — when
            present and link.dutchie_discount_id is set, sends an UPDATE
            (Id=existing); otherwise sends a CREATE (Id=0).
        """
        self.ensure_one()
        existing_id = 0
        if link and link.dutchie_discount_id:
            try:
                existing_id = int(link.dutchie_discount_id)
            except (TypeError, ValueError):
                pass
        # Fall back to the legacy single-valued field if the link doesn't
        # yet have an id (e.g. first publish for this store).
        if not existing_id and self.dutchie_discount_id:
            cur = (self.dutchie_discount_id or '').strip()
            if cur.isdigit():
                existing_id = int(cur)

        # Brand/category/product/etc. — emit Dutchie cross-reference IDs only.
        # Records without a cross-reference drop silently rather than emit
        # wrong-namespace IDs the resolver would miss. Same convention used
        # by `ptl_day._discount_to_webhook_payload`.
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
            'Monday': bool(self.monday),
            'Tuesday': bool(self.tuesday),
            'Wednesday': bool(self.wednesday),
            'Thursday': bool(self.thursday),
            'Friday': bool(self.friday),
            'Saturday': bool(self.saturday),
            'Sunday': bool(self.sunday),
            'DiscountMenuDisplayDetails': {
                'MenuDisplayName': self.menu_display_name or self.name or '',
                'MenuDisplayImageUrl': self.menu_display_image_url or '',
            },
        }

    # ── Per-(discount, store) push — shared by PTL flow + manual button ──
    def _publish_to_one_store(self, store):
        """Publish this discount to one store. Honors mode toggle, writes
        audit log + link table, fires HTTP if live. Returns the link record.

        Same contract as step 1's `mint.ptl.day._push_one_discount` but
        operates on `mint.discount` so both PTL and manual flows share it.
        """
        self.ensure_one()
        Log = self.env['mint.dutchie.discount.push.log'].sudo()
        Link = self.env['mint.discount.dutchie.link'].sudo()
        PtlDay = self.env['mint.ptl.day']  # for the mode/url/api_key getters

        link = Link.search([
            ('discount_id', '=', self.id),
            ('location_id', '=', store.id),
        ], limit=1)
        if not link:
            link = Link.create({
                'discount_id': self.id,
                'location_id': store.id,
                'state': 'pending',
            })
        link.attempts += 1

        mode = PtlDay._get_dutchie_push_mode()
        if mode == 'off':
            link.write({'state': 'skipped', 'last_error': 'mode=off'})
            return link

        loc_id = store.dutchie_loc_id or 0
        lsp_id = store.dutchie_lsp_id or 0
        if not loc_id or not lsp_id:
            link.write({'state': 'failed', 'last_error': 'store missing dutchie_loc_id or dutchie_lsp_id'})
            return link

        url = PtlDay._get_dutchie_push_url()
        api_key = PtlDay._get_dutchie_push_api_key()

        body = {
            'locId': loc_id,
            'lspId': lsp_id,
            'discount': self._to_dutchie_payload(store, link=link),
        }
        log_vals = {
            'discount_id': self.id,
            'company_id': store.id,
            'dutchie_loc_id': str(loc_id),
            'mode': mode,
            'request_payload': json.dumps(body, default=str)[:8000],
            'success': False,
        }

        if mode == 'dry-run':
            log_vals['success'] = True
            log_vals['response_body'] = '(dry-run — no HTTP call)'
            Log.create(log_vals)
            link.write({'state': 'skipped', 'last_error': 'dry-run mode (no write)'})
            return link

        # mode == 'live'
        import time as _t
        t0 = _t.time()
        success = False
        new_id = None
        err = None
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(body).encode('utf-8'),
                headers={
                    'Content-Type': 'application/json',
                    'X-API-Key': api_key,
                    'User-Agent': 'mint-odoo-dutchie-push/2.0',
                },
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read().decode('utf-8', errors='replace')
                log_vals['elapsed_ms'] = int((_t.time() - t0) * 1000)
                log_vals['response_body'] = raw[:8000]
                success = 200 <= resp.status < 300
                log_vals['success'] = success
                # Extract returned Dutchie ID for caching.
                try:
                    parsed = json.loads(raw)
                    raw_data = (parsed.get('dutchie_raw') or {}).get('Data')
                    if isinstance(raw_data, int) and raw_data > 0:
                        new_id = raw_data
                except Exception:
                    pass
        except urllib.error.HTTPError as e:
            log_vals['elapsed_ms'] = int((_t.time() - t0) * 1000)
            err_body = ''
            try:
                err_body = e.read().decode('utf-8', errors='replace')[:8000]
            except Exception:
                pass
            log_vals['response_body'] = err_body
            err = f'HTTP {e.code}: {e.reason}'
            log_vals['error_message'] = err
        except Exception as e:
            log_vals['elapsed_ms'] = int((_t.time() - t0) * 1000)
            err = f'{type(e).__name__}: {str(e)[:500]}'
            log_vals['error_message'] = err

        Log.create(log_vals)

        if success:
            link.write({
                'state': 'synced',
                'dutchie_discount_id': str(new_id) if new_id else link.dutchie_discount_id,
                'synced_at': fields.Datetime.now(),
                'last_error': False,
            })
            # Mirror to the legacy single-valued field (mint_api_v2 Char) only
            # when empty or holding a synthetic 'ptl_*' marker.
            cur = (self.dutchie_discount_id or '').strip()
            is_synthetic = cur.startswith('ptl_') or not cur
            if new_id and is_synthetic:
                self.with_context(skip_publish=True).sudo().write(
                    {'dutchie_discount_id': str(new_id)})
        else:
            link.write({'state': 'failed', 'last_error': err or 'unknown failure'})
        return link

    # ── Manual button ────────────────────────────────────────────────────
    def action_publish_to_dutchie(self):
        """Publish each record to Dutchie POS via the mintinvsvc proxy.
        Iterates eligible stores, calls `_publish_to_one_store` for each.
        Respects the global mode toggle — if mode='off', returns a clear
        notification rather than silently no-op'ing.
        """
        synced = 0
        failed = 0
        skipped_off = 0
        for rec in self:
            if not rec._is_publishable():
                raise UserError(_(
                    'Discount "%s" is not publishable yet — needs a name, '
                    'valid_from, discount_type, and at least one store with '
                    'a Dutchie LocId.'
                ) % (rec.name or '(unnamed)'))
            for store in rec.store_ids.filtered('dutchie_loc_id'):
                link = rec._publish_to_one_store(store)
                if link.state == 'synced':
                    synced += 1
                elif link.state == 'skipped':
                    skipped_off += 1
                else:
                    failed += 1

        msg_parts = []
        if synced:
            msg_parts.append(f'{synced} synced')
        if failed:
            msg_parts.append(f'{failed} failed')
        if skipped_off:
            msg_parts.append(f'{skipped_off} skipped (mode=off or dry-run)')
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Dutchie publish'),
                'message': ', '.join(msg_parts) or _('no eligible stores'),
                'type': 'success' if not failed else 'warning',
                'sticky': bool(failed),
            },
        }

    # ── Auto-publish on create / write ───────────────────────────────────
    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for rec in records:
            if rec.source == 'manual' and rec._is_publishable():
                rec._mark_links_pending_if_active()
        return records

    def write(self, vals):
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
            # Skip records that were JUST flipped from another source to
            # manual — explicit reclassification, not a publish trigger.
            if not was_manual.get(rec.id) and 'source' in vals:
                continue
            rec._mark_links_pending_if_active()
        return result

    def _mark_links_pending_if_active(self):
        """Ensure a 'pending' link exists per eligible store — but ONLY if
        the global mode is non-'off'. When mode='off' we skip silently
        rather than buffer up records that would publish en masse the
        moment the toggle flips. Operators can hit the manual button to
        force a publish (which itself respects mode).
        """
        self.ensure_one()
        if self.env['mint.ptl.day']._get_dutchie_push_mode() == 'off':
            return
        Link = self.env['mint.discount.dutchie.link'].sudo()
        for store in self.store_ids.filtered('dutchie_loc_id'):
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
            elif link.state in ('synced', 'failed', 'skipped'):
                link.write({'state': 'pending', 'last_error': False})

    # ── Drain cron — picks up pending + retries failed ───────────────────
    @api.model
    def _cron_retry_dutchie_publish(self):
        """Drain pending/failed links via `_publish_to_one_store`. Caps
        per-link attempts at 5 to prevent hammering Dutchie on
        fundamentally-broken records; operator can hit the manual button
        to retry beyond that cap.
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
                rec._publish_to_one_store(link.location_id)
                self.env.cr.commit()
                published += 1
            except Exception as e:
                _logger.warning('Dutchie publish cron failed for link %s: %s', link.id, e)
                self.env.cr.rollback()
        _logger.info('Dutchie publish cron: %d/%d processed', published, len(stale))


class PtlDayPushDelegate(models.Model):
    """Delegate per-(discount, store) push from PTL flow to the shared
    `mint.discount._publish_to_one_store`. Two benefits:
      1. PTL flow uses the comprehensive payload + correct LocId/LspId
         (step 1's stub had `int(store.dutchie_store_id)` which is the
         consumer UUID and always returned 0 — Dutchie would have rejected
         once mode flipped to 'live').
      2. PTL pushes are tracked in `mint.discount.dutchie.link` alongside
         manual ones, giving a uniform per-store sync state view.
    """
    _inherit = 'mint.ptl.day'

    def _deal_to_dutchie_payload(self, discount, store):
        # Step 1's payload builder is preserved as-is in dutchie_discount_push.py
        # for backwards compatibility, but anything calling it goes through us.
        return discount._to_dutchie_payload(store)

    def _push_one_discount(self, discount, store, mode, url, api_key, Log):
        # Delegate to the shared per-store push. The mode/url/api_key/Log
        # args from the parent are intentionally unused — `_publish_to_one_store`
        # re-derives them so PTL and manual flows behave identically.
        discount._publish_to_one_store(store)
