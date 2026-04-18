# -*- coding: utf-8 -*-
"""
Debounced push queue for Odoo → mintinvsvc Redis updates.

Why a queue + cron instead of direct on-write threads:
  - Bulk admin edits (e.g. activating 50 deals at once) would otherwise
    fire N pushes/store/sec and burn the mintinvsvc rate budget. The
    queue collapses bursts: each (model, target_key) pair coalesces
    until it ages past the debounce window, then a single push goes out.
  - Threaded fire-and-forget (the PTL pattern) loses writes on Odoo
    worker recycle. The queue persists across restarts.
  - One cron entry, one network egress path — easier to monitor.

Two target_kind values:
  - 'discount_location' → push the full active-discount set for one
    res.company. target_key = company.id (str).
  - 'product_override'  → push override fields for one product. The
    product's dutchie_product_id is target_key; we look up affected
    locations from store_ids on linked discounts AND from all
    is_dispensary companies (overrides are global to the catalog,
    location-scoped on apply).
"""
import json
import logging
from datetime import timedelta
from urllib import request as urlrequest, error as urlerror

from odoo import api, fields, models, _

_logger = logging.getLogger(__name__)

DEBOUNCE_SECONDS = 20
PROCESS_BATCH = 50
HTTP_TIMEOUT = 30

CONFIG_URL = 'mint_redis_push.url'
CONFIG_KEY = 'mint_redis_push.api_key'
CONFIG_ENABLED = 'mint_redis_push.enabled'
DEFAULT_URL = 'https://mintinvsvc-production-6aa5.up.railway.app'


class MintRedisPushQueue(models.Model):
    _name = 'mint.redis.push.queue'
    _description = 'Pending Redis push (Odoo → mintinvsvc)'
    _order = 'enqueued_at asc, id asc'

    target_kind = fields.Selection(
        [('discount_location', 'Discount push for a store'),
         ('product_override', 'Product override push')],
        required=True, index=True,
    )
    target_key = fields.Char(
        required=True, index=True,
        help="discount_location: res.company id (as str). "
             "product_override: dutchie_product_id (as str).",
    )
    enqueued_at = fields.Datetime(
        default=fields.Datetime.now, required=True, index=True,
    )
    attempts = fields.Integer(default=0)
    last_error = fields.Char()

    _sql_constraints = [
        ('uniq_target',
         'UNIQUE(target_kind, target_key)',
         'A push for this target is already queued; coalesce instead of duplicate.'),
    ]

    # ── Public enqueue helpers ─────────────────────────────────────────

    @api.model
    def _enqueue_discount_locations(self, company_ids):
        """Coalesce per-store discount pushes."""
        if not company_ids:
            return
        for cid in set(company_ids):
            self._upsert('discount_location', str(cid))

    @api.model
    def _enqueue_product_override(self, dutchie_product_ids):
        """Coalesce per-product override pushes."""
        if not dutchie_product_ids:
            return
        for pid in set(p for p in dutchie_product_ids if p):
            self._upsert('product_override', str(pid))

    @api.model
    def _upsert(self, kind, key):
        """Idempotent enqueue — refresh enqueued_at to extend the debounce window
        on bursty edits so the push captures the latest state, not a half-edit."""
        existing = self.sudo().search(
            [('target_kind', '=', kind), ('target_key', '=', key)],
            limit=1,
        )
        if existing:
            existing.write({'enqueued_at': fields.Datetime.now()})
        else:
            self.sudo().create({'target_kind': kind, 'target_key': key})

    # ── Cron: drain the queue ──────────────────────────────────────────

    @api.model
    def _cron_process(self):
        """Process pushes whose debounce window has elapsed.
        Scheduled every 30s. Within each tick, take up to PROCESS_BATCH ready
        items so a flood doesn't wedge the cron."""
        if not self._enabled():
            return

        cutoff = fields.Datetime.now() - timedelta(seconds=DEBOUNCE_SECONDS)
        ready = self.sudo().search(
            [('enqueued_at', '<=', cutoff)],
            limit=PROCESS_BATCH,
        )
        if not ready:
            return

        Discount = self.env['mint.discount'].sudo()
        Product = self.env['product.template'].sudo()

        ok, fail = 0, 0
        for q in ready:
            try:
                if q.target_kind == 'discount_location':
                    Discount._push_location_to_redis(int(q.target_key))
                elif q.target_kind == 'product_override':
                    Product._push_override_by_dutchie_id(q.target_key)
                else:
                    raise ValueError(f'unknown target_kind {q.target_kind}')
                q.unlink()
                ok += 1
            except Exception as e:
                fail += 1
                msg = (str(e) or e.__class__.__name__)[:200]
                # Three strikes: stop retrying so a permanently-bad row doesn't
                # block the queue. Surface in attempts/last_error for ops.
                if q.attempts + 1 >= 3:
                    _logger.error('Redis push %s/%s permanently failed after 3 attempts: %s',
                                  q.target_kind, q.target_key, msg)
                    q.unlink()
                else:
                    q.write({'attempts': q.attempts + 1, 'last_error': msg})

        if ok or fail:
            _logger.info('Redis push queue drained: ok=%d fail=%d', ok, fail)

    @api.model
    def _cron_full_resync_discounts(self):
        """Hourly drift repair: re-enqueue every dispensary's discount push.
        Cheap (just queues; the cron above does the work)."""
        if not self._enabled():
            return
        companies = self.env['res.company'].sudo().search([
            ('is_dispensary', '=', True),
            ('dutchie_store_id', '!=', False),
        ])
        self._enqueue_discount_locations(companies.ids)
        _logger.info('Redis push: enqueued full resync for %d stores', len(companies))

    # ── HTTP helper used by both push targets ──────────────────────────

    @api.model
    def _post(self, path, body):
        url = self.env['ir.config_parameter'].sudo().get_param(CONFIG_URL, DEFAULT_URL)
        key = self.env['ir.config_parameter'].sudo().get_param(CONFIG_KEY, '')
        if not key:
            raise ValueError('mint_redis_push.api_key is not configured')
        full = f"{url.rstrip('/')}{path}"
        data = json.dumps(body, default=str).encode('utf-8')
        req = urlrequest.Request(
            full, data=data,
            headers={
                'Content-Type': 'application/json',
                'x-api-key': key,
            },
            method='POST',
        )
        try:
            with urlrequest.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                return resp.status, resp.read().decode('utf-8', 'replace')
        except urlerror.HTTPError as e:
            body_text = ''
            try:
                body_text = e.read().decode('utf-8', 'replace')
            except Exception:
                pass
            raise RuntimeError(f'HTTP {e.code} from {path}: {body_text[:200]}') from e

    @api.model
    def _enabled(self):
        v = self.env['ir.config_parameter'].sudo().get_param(CONFIG_ENABLED, 'True')
        return str(v).lower() not in ('false', '0', 'off', 'no')
