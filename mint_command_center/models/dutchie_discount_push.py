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

    def _deal_to_dutchie_payload(self, discount, store):
        """Translate (mint.discount, res.company) → Dutchie update-discount-item Discount object.

        Mirrors the shape mintinvsvc's POST /api/admin/discounts expects in
        its `discount` body field (see __tests__/fixtures/discount-381839.json
        for the canonical example). Re-pushes set `Id` to the cached Dutchie
        id so we get update-mode rather than create-mode.

        v1 ships only the fields needed for the most common Mint promo
        shapes (percent off, set price, BOGO, bundle). Restrictions,
        weekly recurrence, and SKU-level scoping land in v2 once the
        canary store has been clean for a week.
        """
        calc_method_map = {
            'percent': 'PERCENT_OFF',
            'fixed': 'AMOUNT_OFF',
            'price': 'SET_PRICE',
            'bogo': 'BOGO',
            'bundle': 'BUNDLE',
            'points_multiplier': 'PERCENT_OFF',  # closest fallback
            'clearance': 'PERCENT_OFF',
        }
        # dutchie_discount_id is a Char (defined in mint_api_v2). For Dutchie's
        # update-discount-item we need an integer Id (0 = create new). PTL-derived
        # rows carry a synthetic 'ptl_<n>' value here that's NOT a Dutchie id; treat
        # those as 0 (create) and let the live response give us the real one.
        existing = (discount.dutchie_discount_id or '').strip()
        existing_int = int(existing) if existing.isdigit() else 0
        return {
            'Id': existing_int,
            'Name': (discount.name or '')[:120],
            'LocId': int(store.dutchie_store_id) if store.dutchie_store_id and str(store.dutchie_store_id).isdigit() else 0,
            'IsActive': bool(discount.is_active) if hasattr(discount, 'is_active') else True,
            'IsAvailableOnline': True,
            'CalculationMethod': calc_method_map.get(discount.discount_type, 'PERCENT_OFF'),
            'Amount': float(discount.discount_value or 0.0),
            # Day-of-week recurrence — Mint marketing flips these via the
            # PTL day binding; the Dutchie shape uses a WeeklyRecurrenceInfo
            # block that v2 will fill. v1 leaves it empty (= every day).
            'WeeklyRecurrenceInfo': None,
            'Restrictions': [],
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

    def _push_one_discount(self, discount, store, mode, url, api_key, Log):
        """Build payload, log, and (in 'live' mode only) POST to mintinvsvc."""
        payload = {
            'locId': int(store.dutchie_store_id) if store.dutchie_store_id and str(store.dutchie_store_id).isdigit() else 0,
            'lspId': 0,  # v1: not yet captured per-store; mintinvsvc resolves from locId
            'discount': self._deal_to_dutchie_payload(discount, store),
        }
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
