import json
import logging
import threading
import urllib.request
from datetime import timedelta

from odoo import api, fields, models

from odoo.addons.mint_api_v2.models.discount_canonical import (
    calc_method_string_for,
    discount_value_for,
)

from .deal_mixins import coerce_dutchie_ids

_logger = logging.getLogger(__name__)

WEBHOOK_URL_PARAM = 'mint.ptl_sync.webhook_url'
DEFAULT_WEBHOOK_URL = 'https://mintinvsvc-production-6aa5.up.railway.app/api/webhook/ptl-discount-sync'
API_KEY_PARAM = 'mint.inventory_service.api_key'

DAY_NAME_MAP = {
    0: 'monday', 1: 'tuesday', 2: 'wednesday', 3: 'thursday',
    4: 'friday', 5: 'saturday', 6: 'sunday',
}

DISCOUNT_TYPE_MAP = {
    'percent': 'percent',
    'fixed': 'fixed',
    'bogo': 'bogo',
    'bundle': 'bogo',
    'price': 'price_to_amount',
    'points_multiplier': 'points_multiplier',
    'clearance': 'clearance',
}

# CALC_METHOD_MAP removed (Dutchie-parity, 2026-08).
#
# It emitted canonical strings Dutchie has no concept of — 'BOGO',
# 'DOLLAR_OFF', 'POINTS_MULTIPLIER', 'CLEARANCE_PERCENT_OFF' — and defaulted
# everything else to PERCENT_OFF. Verified against 1,477 Dutchie-sourced
# discounts: Dutchie exposes exactly six CalculationMethodIds
# (1 FLAT_AMOUNT_OFF, 2 PERCENT_OFF, 3 PRICE_TO_AMOUNT, 5 DOLLAR_OFF_TOTAL,
# 6 PRICE_TO_AMOUNT_TOTAL, 15 LOYALTY) and there is no BOGO method — a BOGO is
# encoded as PRICE_TO_AMOUNT with threshold_min=2 and the "get" item's price
# (Dutchie uses $0.01 for a free one).
#
# discount_canonical is the single source of truth, shared with the Dutchie
# push and mint_redis_push and drift-guarded against the mintinvsvc copy.


class PtlDay(models.Model):
    _name = 'mint.ptl.day'
    _description = 'PTL Day — Daily promotional schedule'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'date desc'

    name = fields.Char(
        string='Name',
        compute='_compute_name',
        store=True,
        readonly=False,
    )
    date = fields.Date(
        string='Date',
        required=True,
        index=True,
        tracking=True,
    )
    market_id = fields.Many2one(
        'mint.region',
        string='Market',
        required=True,
        index=True,
        tracking=True,
        default=lambda self: self._default_market_id(),
        help='Market/region this PTL day targets',
    )
    state = fields.Selection(
        selection=[
            ('draft', 'Draft'),
            ('confirmed', 'Confirmed'),
            ('published', 'Published'),
        ],
        string='Status',
        default='draft',
        tracking=True,
    )
    deal_ids = fields.Many2many(
        'mint.ptl.deal',
        'mint_ptl_day_deal_rel',
        'day_id',
        'deal_id',
        string='Deals',
    )
    deal_count = fields.Integer(
        string='Deal Count',
        compute='_compute_deal_count',
        store=True,
    )
    notes = fields.Text(string='Notes')

    # Odoo 19 reads models.Constraint; the legacy `_sql_constraints = [...]`
    # list is SILENTLY IGNORED. This constraint was declared the old way and so
    # was never created in the database — verified 2026-08-24: the table carried
    # only its 5 auto-generated foreign keys, and mint_command_center had 226
    # constraint records registered, every one a foreign key and not a single
    # declared unique. (mint_api_v2, which already uses models.Constraint, has
    # its 3 unique constraints installed — same database, same Odoo.)
    #
    # The cost of that silence: schedule_deal does a plain check-then-create and
    # its own docstring says "the date_market_uniq constraint guarantees at most
    # one". Nothing guaranteed it, so concurrent calls both inserted — 105
    # duplicate (date, market) rows accumulated, 104 of them created within the
    # same minute as their twin. Those were merged on 2026-08-24 (7,105 deal
    # links preserved into the survivors, 769 -> 664 rows) so this can finally
    # take effect.
    _date_market_uniq = models.Constraint(
        'UNIQUE(date, market_id)',
        'Only one PTL day per date per market is allowed.',
    )

    @api.model
    def _default_market_id(self):
        return self.env['mint.region'].search([('code', '=', 'AZ')], limit=1).id

    @api.depends('date', 'market_id')
    def _compute_name(self):
        for rec in self:
            code = rec.market_id.code if rec.market_id else ''
            if rec.date:
                rec.name = f"{rec.date} [{code}]" if code else str(rec.date)
            else:
                rec.name = 'New PTL Day'

    @api.depends('deal_ids')
    def _compute_deal_count(self):
        for rec in self:
            rec.deal_count = len(rec.deal_ids)

    # ─── Dynamic Store UUID Map ──────────────────────────────────────────

    def _get_store_uuid_map(self, market=None):
        """Build company_id → Dutchie UUID map, optionally narrowed to a market.

        `market` is an explicit argument rather than a read of `self.market_id`
        because the crons call this through an EMPTY mint.ptl.day recordset,
        where `self.market_id` is silently falsy — which is how Arizona deals
        reached every market on 2026-08-25. Callers must now say which market
        they mean; passing None still means "every dispensary" but has to be
        chosen rather than fallen into.
        """
        domain = [
            ('is_dispensary', '=', True),
            ('dutchie_store_id', '!=', False),
        ]
        if market:
            domain.append(('region_id', '=', market.id))
        stores = self.env['res.company'].sudo().search(domain)
        return {s.id: s.dutchie_store_id for s in stores if s.dutchie_store_id}

    # ─── Daily Cron (called via ir.cron on mint.ptl.day) ─────────────────

    @api.model
    def _cron_daily_lifecycle(self):
        """Delegates to mint.discount._cron_ptl_daily_lifecycle()."""
        self.env['mint.discount']._cron_ptl_daily_lifecycle()

    # ─── Publish Action ──────────────────────────────────────────────────

    def action_publish(self):
        """Publish: sync deals → mint.discount, compute day-of-week, push to Redis."""
        self.ensure_one()

        Discount = self.env['mint.discount'].sudo()
        discount_ids = []

        # Mark this day published BEFORE recomputing day-of-week booleans.
        # _recompute_day_booleans only counts days in state='published', so if
        # this write ran afterward the day being published would be excluded
        # from its own recompute — dropping its weekday from the discount and
        # hiding that day (e.g. the last window of a non-contiguous deal) from
        # the Daily Deals page. (Odoo #93649 AC05.)
        self.write({'state': 'published'})

        # Exclude expired deals from publish — a deal whose end date is in the
        # past must never reach the storefront (Odoo #94502 phase 2). Uses the
        # is_expired helper on mint.ptl.deal added in phase 1.
        active_deals = self.deal_ids.filtered(lambda d: not d.is_expired)
        skipped_expired = len(self.deal_ids) - len(active_deals)

        for deal in active_deals:
            discount = self._ensure_discount(deal)
            Discount._recompute_day_booleans(discount)
            discount_ids.append(discount.id)

        # Push to inventory service
        self._push_discounts_to_redis(discount_ids)

        # Push to Dutchie POS (gated by mint.dutchie_discount_push.mode +
        # per-market + per-store flags; no-ops with mode='off', which is
        # the default until ops flips it).
        self._push_discounts_to_dutchie(discount_ids)

        self.message_post(
            body=f"Published {len(discount_ids)} deal(s) to frontend."
                 + (f" Skipped {skipped_expired} expired deal(s)." if skipped_expired else ""),
            message_type='comment',
        )

    def action_unpublish(self):
        """Unpublish: drop this day's deals from the FE.

        For each deal on this day, look for OTHER days that are still
        'published' and also reference the deal. If any exist, rescope the
        mint.discount to those days' date range + day-of-week bools so the
        deal keeps serving for them. If no other published day remains,
        deactivate the mint.discount outright. Either way, push the updated
        records to mintinvsvc so Redis reflects the change on the next
        cacheSync tick.
        """
        self.ensure_one()
        if self.state != 'published':
            from odoo.exceptions import UserError
            raise UserError("Only published days can be unpublished.")

        Day = self.env['mint.ptl.day'].sudo()
        affected_ids = []
        deactivated = 0
        rescoped = 0

        for deal in self.deal_ids:
            discount = deal.discount_id
            if not discount:
                continue

            other_days = Day.search([
                ('id', '!=', self.id),
                ('state', '=', 'published'),
                ('deal_ids', 'in', deal.id),
            ])

            if other_days:
                dates = other_days.mapped('date')
                discount.write({
                    'is_published': True,
                    'valid_from': min(dates),
                    'valid_until': max(dates),
                })
                self.env['mint.discount']._recompute_day_booleans(discount)
                rescoped += 1
            else:
                discount.write({'is_published': False})
                deactivated += 1

            affected_ids.append(discount.id)

        if affected_ids:
            self._push_discounts_to_redis(affected_ids)

        self.write({'state': 'confirmed'})
        self.message_post(
            body=(
                f"Unpublished: {deactivated} deal(s) deactivated, "
                f"{rescoped} rescoped to remaining published days."
            ),
            message_type='comment',
        )

    def action_open_stock_check(self):
        """Open stock check wizard pre-filled with this day's deals."""
        return {
            'name': 'Check Stock',
            'type': 'ir.actions.act_window',
            'res_model': 'mint.stock.check.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_ptl_day_id': self.id,
                'default_deal_ids': [(6, 0, self.deal_ids.ids)],
                'default_market_id': self.market_id.id if self.market_id else False,
            },
        }

    def _ensure_discount(self, deal):
        """Create or update the mint.discount record for a PTL deal."""
        Discount = self.env['mint.discount'].sudo()
        vals = self._deal_to_discount_vals(deal)

        if deal.discount_id:
            deal.discount_id.write(vals)
            return deal.discount_id

        vals['source'] = 'ptl'
        discount = Discount.create(vals)
        deal.write({'discount_id': discount.id})
        return discount

    def _deal_to_discount_vals(self, deal):
        """Map ptl.deal fields → mint.discount values."""
        disc_type = DISCOUNT_TYPE_MAP.get(deal.discount_type, 'percent')

        # Normalize discount amount
        if disc_type == 'percent' and deal.discount_value > 1:
            amount = deal.discount_value / 100.0
        else:
            amount = deal.discount_value

        vals = {
            'name': deal.name,
            'description': deal.sales_details or '',
            'terms': deal.details_exclusions or '',
            'discount_type': disc_type,
            'discount_amount': amount,
            'is_published': True,
            'is_featured': deal.is_featured,
            'is_available_online': deal.is_available_online,
            'ptl_deal_id': deal.id,
            'dutchie_discount_id': f'ptl_{deal.id}',
            'excluded_skus': deal.excluded_skus or False,
        }

        # Structured BOGO/bundle mirror (#93677 Cluster C, per #94040 Path B):
        # the Dutchie push reads mint.discount.threshold_min/discount_amount,
        # so the new structured fields must land there. BOGO → item threshold
        # = buy+get, amount = fractional get-discount; bundle → first tier's
        # qty/price. Un-backfilled deals (zero qtys / no tiers) keep the
        # legacy mapping above.
        #
        # threshold_min/calculation_method_id are reset unconditionally:
        # _ensure_discount write()s these vals onto an existing record, and a
        # key omitted from vals would leave a stale threshold/calc id behind
        # when a deal is later de-structured (tiers deleted, type changed).
        # calculation_method_id lives on mint_dutchie_discount_mirror, which
        # mint_command_center doesn't depend on — guard on the field map.
        Discount = self.env['mint.discount']
        has_calc_field = 'calculation_method_id' in Discount._fields
        vals['threshold_min'] = 0
        if has_calc_field:
            vals['calculation_method_id'] = 0
        if deal.discount_type == 'bogo' and deal.bogo_buy_qty and deal.bogo_get_qty:
            vals['threshold_min'] = deal.bogo_buy_qty + deal.bogo_get_qty
            # Dutchie has no BOGO calculation method. Verified against its own
            # data: "BOGO $.01 Wyld - All Products" arrives as
            # CalculationMethodId 3 (PRICE_TO_AMOUNT), threshold_min 2,
            # discount_amount 0.01 — i.e. the price the "get" item drops to,
            # not a percentage. Emitting a fraction here (the previous
            # behaviour) made the register read "$1.00" or, with the method
            # unresolved, "percent off 0" = no discount at all.
            get_price = None
            if deal.original_price:
                get_price = round(
                    deal.original_price * (1.0 - (deal.bogo_get_pct or 1.0)), 2)
                if get_price <= 0:
                    # Dutchie's convention for a free "get" item.
                    get_price = 0.01
            if has_calc_field and get_price is not None:
                vals['discount_amount'] = get_price
                vals['calculation_method_id'] = 3
            else:
                # No original_price means the resulting price is unknowable.
                # Fabricating one would misprice at the register, so keep the
                # legacy value and leave the method unset for the sentinel to
                # keep reporting.
                vals['discount_amount'] = deal.bogo_get_pct or 1.0
        elif deal.discount_type == 'bundle' and deal.bundle_tier_ids:
            # One2many is _order='sequence, id' — first row IS the first tier.
            first = deal.bundle_tier_ids[0]
            if has_calc_field:
                # Calc id 6 = PRICE_TO_AMOUNT_TOTAL ("N for $X": amount is
                # the bundle TOTAL, threshold_min is N) — without the explicit
                # id the push falls back to PERCENT_OFF and would publish the
                # dollar price as a percent (review finding, 2026-06-12).
                vals['threshold_min'] = first.qty
                vals['discount_amount'] = first.price
                vals['calculation_method_id'] = 6
            # Without the canonical-registry field there is no way to label
            # the price correctly — keep the legacy amount (discount_value)
            # rather than feeding a dollar price into a percent slot.

        # Date range from linked PTL days
        day_dates = deal.day_ids.mapped('date')
        if day_dates:
            vals['valid_from'] = min(day_dates)
            vals['valid_until'] = max(day_dates)

        # Store targeting
        if deal.store_ids:
            vals['store_ids'] = [(6, 0, deal.store_ids.ids)]

        # Brand targeting — direct mint.brand reference. Multi-brand
        # (#93635): every brand on the deal lands on the discount.
        deal_brands = (deal.brand_ids | deal.brand_id) if deal.brand_id else deal.brand_ids
        if deal_brands:
            vals['brand_ids'] = [(6, 0, deal_brands.ids)]

        if deal.excluded_brand_ids:
            vals['exclude_brand_ids'] = [(6, 0, deal.excluded_brand_ids.ids)]

        # Category targeting (ptl.deal uses char, mint.discount uses product.category)
        # Uses the master-category resolver on the deal so master buckets like
        # "Flower" expand to Prepack Flower / Bulk Flower / etc. — otherwise a
        # freshly bucketed deal would only match the bare-named "Flower" cats
        # and miss most SKUs (regression observed 2026-05-18).
        if deal.product_category:
            cats = deal._resolve_master_categories()
            if cats:
                vals['category_ids'] = [(6, 0, cats.ids)]

        # Explicit per-product restriction. mint.discount._match_product
        # already intersects: `if self.product_ids and product.id not in
        # self.product_ids.ids: return False`, so passing product_ids
        # narrows the discount without widening anything else.
        if deal.explicit_product_ids:
            vals['product_ids'] = [(6, 0, deal.explicit_product_ids.ids)]

        return vals

    # ─── Webhook Push to Inventory Service ───────────────────────────────

    def _push_discounts_to_redis(self, discount_ids):
        """Push PTL discounts to inventory service (fire-and-forget)."""
        if not discount_ids:
            return

        discounts = self.env['mint.discount'].sudo().browse(discount_ids)

        # Every store, for resolving a discount's own store_ids to UUIDs. The
        # per-market maps below decide who an UNSCOPED discount reaches.
        all_store_uuids = self._get_store_uuid_map()
        if not all_store_uuids:
            _logger.warning('PTL publish: no stores with Dutchie UUIDs found')
            return

        market_uuid_cache = {}

        def uuids_for_market(market):
            if market.id not in market_uuid_cache:
                market_uuid_cache[market.id] = self._get_store_uuid_map(market)
            return market_uuid_cache[market.id]

        # Group discounts by store UUID
        store_payloads = {}  # uuid → [discount_dicts]
        skipped_no_market = 0

        for discount in discounts:
            if discount.store_ids:
                # An explicit store list always wins.
                store_ids = discount.store_ids.ids
            else:
                # 246 of 251 published PTL discounts carry no store_ids. This
                # used to mean "every store in the map" — and because the crons
                # call in on an empty recordset the map was every store in
                # EVERY market, so on 2026-08-25 Arizona deals landed in all 17
                # Florida stores (a compliance-isolation market), plus MI, IL,
                # MO and NV. 5,859 stray rows had to be deleted afterwards.
                #
                # An unscoped discount now reaches its OWN market, taken from
                # the PTL deal it came from, falling back to the market of the
                # day being published. With neither we skip it rather than
                # broadcast: a deal that reaches nobody is a missing card, a
                # deal that reaches everybody is the wrong price in five states.
                market = discount.ptl_deal_id.market_id or self.market_id
                if not market:
                    skipped_no_market += 1
                    _logger.warning(
                        'PTL publish: discount %s (%s) has no store_ids and no '
                        'resolvable market — skipping rather than fanning it '
                        'across every market',
                        discount.id, (discount.name or '')[:60],
                    )
                    continue
                store_ids = list(uuids_for_market(market).keys())

            for store_id in store_ids:
                uuid = all_store_uuids.get(store_id)
                if not uuid:
                    continue
                if uuid not in store_payloads:
                    store_payloads[uuid] = []
                store_payloads[uuid].append(self._discount_to_webhook_payload(discount, uuid))

        if skipped_no_market:
            _logger.warning(
                'PTL publish: skipped %d discount(s) with no store_ids and no market',
                skipped_no_market)

        # Fire webhooks from ONE background thread that walks the stores in
        # sequence — not one thread per store.
        #
        # The per-store version died on 2026-08-25 the first time the restored
        # daily lifecycle cron ran against a two-month backlog: ~600 discounts
        # fanned out over every store, and `thread.start()` raised
        # `RuntimeError: can't start new thread`. That exception propagated out
        # of this method into _cron_ptl_daily_lifecycle, so Odoo rolled the
        # whole cron transaction back and the 329 expired discounts it had just
        # unpublished stayed published. OpenSSL was failing in the same breath
        # ("malloc failure", "ASN1 lib", "internal error") — the same resource
        # exhaustion surfacing through the TLS handshakes.
        #
        # One thread per call is enough: this is a background push with no
        # caller waiting on it, so the stores may as well be sequential, and a
        # single thread cannot exhaust the pool no matter how large the
        # backlog gets.
        get_param = self.env['ir.config_parameter'].sudo().get_param
        webhook_url = get_param(WEBHOOK_URL_PARAM, DEFAULT_WEBHOOK_URL)
        api_key = get_param(API_KEY_PARAM, '')

        payloads = [
            (uuid, json.dumps({
                'location_id': uuid,
                'source': 'ptl',
                'discounts': deals,
            }).encode('utf-8'))
            for uuid, deals in store_payloads.items()
        ]

        def _fire(uuid, data):
            # uuid is a parameter, not a closure over the loop variable — the
            # old code closed over it, so every thread logged whichever store
            # happened to be last and the logs were useless for telling which
            # store actually failed.
            try:
                req = urllib.request.Request(
                    webhook_url, data=data,
                    headers={
                        'Content-Type': 'application/json',
                        'X-API-Key': api_key,
                    },
                )
                with urllib.request.urlopen(req, timeout=30) as resp:
                    _logger.info('PTL webhook %s: %s', uuid[:12], resp.status)
            except Exception as e:
                _logger.warning('PTL webhook failed for %s: %s', uuid[:12], e)

        def _dispatch():
            for uuid, data in payloads:
                _fire(uuid, data)

        # Belt and braces: even a single start() can fail under memory
        # pressure, and pushing to Redis must never be able to roll back the
        # caller's writes. Fall back to sending inline rather than losing the
        # push entirely.
        try:
            thread = threading.Thread(target=_dispatch, daemon=True)
            thread.start()
        except RuntimeError as e:
            _logger.warning(
                'PTL publish: could not start webhook thread (%s) — sending inline', e)
            try:
                _dispatch()
            except Exception:
                _logger.exception('PTL publish: inline webhook dispatch failed')

        _logger.info('PTL publish: queued webhooks for %d stores, %d discounts',
                      len(store_payloads), len(discount_ids))

    def _discount_to_webhook_payload(self, discount, location_uuid):
        """Convert mint.discount → inventory service webhook payload."""
        calc_method = calc_method_string_for(discount)

        # Build brand/category/product targeting JSONB.
        # Emit the Dutchie-namespace cross-reference IDs (dutchie_brand_id /
        # dutchie_category_id / dutchie_product_id) that Odoo records carry.
        # The downstream mintinvsvc resolver indexes inventory by those same
        # IDs — Odoo-internal record ids have no meaning downstream. Records
        # without a cross-reference are dropped from the payload so we never
        # emit wrong-namespace IDs the resolver would silently miss.
        brands = None
        if discount.brand_ids:
            dutchie_ids = coerce_dutchie_ids(discount.brand_ids, 'dutchie_brand_id')
            if dutchie_ids:
                brands = {'ids': dutchie_ids, 'isExclusion': False}
        elif discount.exclude_brand_ids:
            dutchie_ids = coerce_dutchie_ids(discount.exclude_brand_ids, 'dutchie_brand_id')
            if dutchie_ids:
                brands = {'ids': dutchie_ids, 'isExclusion': True}

        categories = None
        if discount.category_ids:
            dutchie_ids = coerce_dutchie_ids(discount.category_ids, 'dutchie_category_id')
            if dutchie_ids:
                categories = {'ids': dutchie_ids, 'isExclusion': False}

        products = None
        if discount.product_ids:
            dutchie_ids = coerce_dutchie_ids(discount.product_ids, 'dutchie_product_id')
            if dutchie_ids:
                products = {'ids': dutchie_ids, 'isExclusion': False}

        excluded_skus = sorted(discount._excluded_sku_set()) if discount.excluded_skus else None

        return {
            'source_external_id': str(discount.id),
            'discount_id': discount.id + 100000,
            'discount_name': discount.name,
            'discount_code': discount.code or None,
            # discount_value_for() reads whichever field the calc method
            # stores the number in — ids 1/5/15 keep it in discount_value and
            # their discount_amount is 0, so reading discount_amount directly
            # published those deals as a no-op.
            'discount_amount': discount_value_for(discount),
            'calculation_method': calc_method,
            # Minimum-item threshold (#93677): N of "N for $X" bundles and
            # buy+get of structured BOGOs. 0 for legacy/non-threshold deals.
            'threshold_min': discount.threshold_min or 0,
            'is_active': discount.is_active,
            'is_published': discount.is_published,
            'is_available_online': discount.is_available_online,
            'start_time': discount.start_time or 0.0,
            'end_time': discount.end_time or 0.0,
            'valid_from': discount.valid_from.isoformat() if discount.valid_from else None,
            'valid_until': discount.valid_until.isoformat() if discount.valid_until else None,
            'monday': discount.monday,
            'tuesday': discount.tuesday,
            'wednesday': discount.wednesday,
            'thursday': discount.thursday,
            'friday': discount.friday,
            'saturday': discount.saturday,
            'sunday': discount.sunday,
            'brands': brands,
            'product_categories': categories,
            'products': products,
            'excluded_skus': excluded_skus,
            'sales_details': discount.description or None,
            'deal_classification': discount.deal_classification or 'sale',
        }

    def action_export_ptl_calendar_csv(self):
        """Return an act_url action that streams the selected PTL days as CSV.

        Wired from the `PTL Calendar (CSV)` server action defined in
        `reports/ptl_calendar_reports.xml`.
        """
        ids_param = ','.join(str(r.id) for r in self)
        return {
            'type': 'ir.actions.act_url',
            'url': f'/mint/ptl-calendar/export.csv?ids={ids_param}',
            'target': 'self',
        }

    # ─── Text Deals export (SMS / POS paste) ─────────────────────────────

    def _compose_text_deals(self):
        """Render this day's deals as a plain-text block suitable for SMS,
        bud-tender clipboard paste, or POS terminal pinning.

        One line per deal, ordered by sequence then id (matching the on-day
        kanban order). Empty if the day has no deals.
        """
        self.ensure_one()
        deals = self.deal_ids.sorted(lambda d: (d.sequence, d.id))
        return '\n'.join(d._compose_text_deal_line() for d in deals)

    def action_open_text_deals_wizard(self):
        """Open the export wizard pre-filled with this day."""
        self.ensure_one()
        return {
            'name': 'Export Text Deals',
            'type': 'ir.actions.act_window',
            'res_model': 'mint.ptl.text.deals.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_day_id': self.id},
        }

    @api.model
    def plotted_dates_for_market(self, market_id, date_from, date_to):
        """Return the ISO dates (YYYY-MM-DD) that already have a mint.ptl.day
        for the given market within [date_from, date_to] inclusive.

        Read-only helper for the ptl_day_grid widget — it shades days that are
        already on the PTL for this market so the reviewer sees existing load
        while clicking days for a new deal. Returns [] when no market is set.
        """
        if not market_id:
            return []
        rows = self.search_read(
            [
                ('market_id', '=', int(market_id)),
                ('date', '>=', date_from),
                ('date', '<=', date_to),
            ],
            ['date'],
        )
        return [str(r['date']) for r in rows]

    @api.model
    def schedule_deal(self, deal_id, date, market_id):
        """Schedule an approved deal on a given (date, market) PTL day.

        - Finds the existing mint.ptl.day for that (date, market_id), or
          creates a new draft day if none exists (the date_market_uniq
          constraint guarantees at most one).
        - Adds the deal to deal_ids (idempotent — m2m link, no dup).
        - Posts a chatter note on the day so the action is auditable.

        Called from the PTL Calendar dragboard side pane when the user drops
        an approved-deal card onto a calendar cell.
        """
        if not deal_id or not date or not market_id:
            from odoo.exceptions import UserError
            raise UserError("schedule_deal requires deal_id, date, and market_id.")

        Deal = self.env['mint.ptl.deal']
        deal = Deal.browse(int(deal_id)).exists()
        if not deal:
            from odoo.exceptions import UserError
            raise UserError(f"Deal {deal_id} not found.")
        if deal.state != 'approved':
            from odoo.exceptions import UserError
            raise UserError(
                f"Only approved deals can be scheduled from the dragboard "
                f"(deal {deal.id} is '{deal.state}')."
            )

        day = self.search([
            ('date', '=', date),
            ('market_id', '=', int(market_id)),
        ], limit=1)
        created = False
        if not day:
            day = self.create({'date': date, 'market_id': int(market_id)})
            created = True

        if deal.id not in day.deal_ids.ids:
            day.write({'deal_ids': [(4, deal.id)]})
            day.message_post(
                body=f"Scheduled deal <b>{deal.name}</b> via PTL Calendar dragboard.",
                message_type='comment',
            )

        return {
            'day_id': day.id,
            'deal_id': deal.id,
            'created': created,
        }
