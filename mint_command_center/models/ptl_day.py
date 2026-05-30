import json
import logging
import threading
import urllib.request
from datetime import timedelta

from odoo import api, fields, models

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

CALC_METHOD_MAP = {
    'percent': 'PERCENT_OFF',
    'fixed': 'DOLLAR_OFF',
    'bogo': 'BOGO',
    'price_to_amount': 'PRICE_TO_AMOUNT_TOTAL',
    'points_multiplier': 'POINTS_MULTIPLIER',
    'clearance': 'CLEARANCE_PERCENT_OFF',
}


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

    _sql_constraints = [
        ('date_market_uniq', 'unique(date, market_id)',
         'Only one PTL day per date per market is allowed.'),
    ]

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

    def _get_store_uuid_map(self):
        """Build company_id → Dutchie UUID map from res.company records."""
        domain = [
            ('is_dispensary', '=', True),
            ('dutchie_store_id', '!=', False),
        ]
        if self.market_id:
            domain.append(('region_id', '=', self.market_id.id))
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

        for deal in self.deal_ids:
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
            body=f"Published {len(discount_ids)} deal(s) to frontend.",
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
            'is_available_online': True,
            'ptl_deal_id': deal.id,
            'dutchie_discount_id': f'ptl_{deal.id}',
            'excluded_skus': deal.excluded_skus or False,
        }

        # Date range from linked PTL days
        day_dates = deal.day_ids.mapped('date')
        if day_dates:
            vals['valid_from'] = min(day_dates)
            vals['valid_until'] = max(day_dates)

        # Store targeting
        if deal.store_ids:
            vals['store_ids'] = [(6, 0, deal.store_ids.ids)]

        # Brand targeting — direct mint.brand reference
        if deal.brand_id:
            vals['brand_ids'] = [(6, 0, [deal.brand_id.id])]

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
        store_uuid_map = self._get_store_uuid_map()

        if not store_uuid_map:
            _logger.warning('PTL publish: no stores with Dutchie UUIDs found for market %s',
                            self.market_id.code if self.market_id else '(none)')
            return

        # Group discounts by store UUID
        store_payloads = {}  # uuid → [discount_dicts]

        for discount in discounts:
            store_ids = discount.store_ids.ids if discount.store_ids else list(store_uuid_map.keys())
            for store_id in store_ids:
                uuid = store_uuid_map.get(store_id)
                if not uuid:
                    continue
                if uuid not in store_payloads:
                    store_payloads[uuid] = []
                store_payloads[uuid].append(self._discount_to_webhook_payload(discount, uuid))

        # Fire webhook per store (async, fire-and-forget)
        get_param = self.env['ir.config_parameter'].sudo().get_param
        webhook_url = get_param(WEBHOOK_URL_PARAM, DEFAULT_WEBHOOK_URL)
        api_key = get_param(API_KEY_PARAM, '')

        for uuid, deals in store_payloads.items():
            payload = json.dumps({
                'location_id': uuid,
                'source': 'ptl',
                'discounts': deals,
            }).encode('utf-8')

            def _fire(url, data, key):
                try:
                    req = urllib.request.Request(
                        url, data=data,
                        headers={
                            'Content-Type': 'application/json',
                            'X-API-Key': key,
                        },
                    )
                    with urllib.request.urlopen(req, timeout=30) as resp:
                        _logger.info('PTL webhook %s: %s', uuid[:12], resp.status)
                except Exception as e:
                    _logger.warning('PTL webhook failed for %s: %s', uuid[:12], e)

            thread = threading.Thread(target=_fire, args=(webhook_url, payload, api_key))
            thread.daemon = True
            thread.start()

        _logger.info('PTL publish: fired webhooks for %d stores, %d discounts',
                      len(store_payloads), len(discount_ids))

    def _discount_to_webhook_payload(self, discount, location_uuid):
        """Convert mint.discount → inventory service webhook payload."""
        calc_method = CALC_METHOD_MAP.get(discount.discount_type, 'PERCENT_OFF')

        # Build brand/category/product targeting JSONB.
        # Emit the Dutchie-namespace cross-reference IDs (dutchie_brand_id /
        # dutchie_category_id / dutchie_product_id) that Odoo records carry.
        # The downstream mintinvsvc resolver indexes inventory by those same
        # IDs — Odoo-internal record ids have no meaning downstream. Records
        # without a cross-reference are dropped from the payload so we never
        # emit wrong-namespace IDs the resolver would silently miss.
        def _coerce_ids(records, field):
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

        brands = None
        if discount.brand_ids:
            dutchie_ids = _coerce_ids(discount.brand_ids, 'dutchie_brand_id')
            if dutchie_ids:
                brands = {'ids': dutchie_ids, 'isExclusion': False}
        elif discount.exclude_brand_ids:
            dutchie_ids = _coerce_ids(discount.exclude_brand_ids, 'dutchie_brand_id')
            if dutchie_ids:
                brands = {'ids': dutchie_ids, 'isExclusion': True}

        categories = None
        if discount.category_ids:
            dutchie_ids = _coerce_ids(discount.category_ids, 'dutchie_category_id')
            if dutchie_ids:
                categories = {'ids': dutchie_ids, 'isExclusion': False}

        products = None
        if discount.product_ids:
            dutchie_ids = _coerce_ids(discount.product_ids, 'dutchie_product_id')
            if dutchie_ids:
                products = {'ids': dutchie_ids, 'isExclusion': False}

        excluded_skus = sorted(discount._excluded_sku_set()) if discount.excluded_skus else None

        return {
            'source_external_id': str(discount.id),
            'discount_id': discount.id + 100000,
            'discount_name': discount.name,
            'discount_code': discount.code or None,
            'discount_amount': discount.discount_amount,
            'calculation_method': calc_method,
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
