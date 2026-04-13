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

        for deal in self.deal_ids:
            discount = self._ensure_discount(deal)
            Discount._recompute_day_booleans(discount)
            discount_ids.append(discount.id)

        # Push to inventory service
        self._push_discounts_to_redis(discount_ids)

        self.write({'state': 'published'})
        self.message_post(
            body=f"Published {len(discount_ids)} deal(s) to frontend.",
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
            'is_active': True,
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

        # Brand targeting (ptl.deal uses res.partner, mint.discount uses mint.brand)
        if deal.brand_id:
            brand = self.env['mint.brand'].search([
                ('name', '=ilike', deal.brand_id.name),
            ], limit=1)
            if brand:
                vals['brand_ids'] = [(6, 0, [brand.id])]

        if deal.excluded_brand_ids:
            excluded = self.env['mint.brand'].search([
                ('name', 'in', deal.excluded_brand_ids.mapped('name')),
            ])
            if excluded:
                vals['exclude_brand_ids'] = [(6, 0, excluded.ids)]

        # Category targeting (ptl.deal uses char, mint.discount uses product.category)
        if deal.product_category:
            cats = self.env['product.category'].search([
                ('name', '=ilike', deal.product_category),
            ])
            if cats:
                vals['category_ids'] = [(6, 0, cats.ids)]

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

        # Build brand/category targeting JSONB
        brands = None
        if discount.brand_ids:
            brands = {'ids': discount.brand_ids.ids, 'isExclusion': False}
        elif discount.exclude_brand_ids:
            brands = {'ids': discount.exclude_brand_ids.ids, 'isExclusion': True}

        categories = None
        if discount.category_ids:
            categories = {'ids': discount.category_ids.ids, 'isExclusion': False}

        products = None
        if discount.product_ids:
            products = {'ids': discount.product_ids.ids, 'isExclusion': False}

        excluded_skus = sorted(discount._excluded_sku_set()) if discount.excluded_skus else None

        return {
            'source_external_id': str(discount.id),
            'discount_id': discount.id + 100000,
            'discount_name': discount.name,
            'discount_code': discount.code or None,
            'discount_amount': discount.discount_amount,
            'calculation_method': calc_method,
            'is_active': discount.is_active,
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
