import calendar as _calendar
import json
import logging
from datetime import date, timedelta

from odoo import fields, http
from odoo.http import request

from ..models.deal_mixins import (
    BOGO_VARIANT_QTYS,
    format_bogo_text,
    format_bundle_tiers_text,
)

_logger = logging.getLogger(__name__)

_ONE_DAY = timedelta(days=1)

PRODUCT_CATEGORIES = [
    ('flower', 'Flower'),
    ('pre-rolls', 'Pre-Rolls'),
    ('vapes', 'Vapes'),
    ('edibles', 'Edibles'),
    ('concentrates', 'Concentrates'),
    ('topicals', 'Topicals'),
    ('accessories', 'Accessories'),
    ('beverages', 'Beverages'),
    ('tinctures', 'Tinctures'),
    ('other', 'Other'),
]

DISCOUNT_TYPES = [
    ('percent', 'Percentage Off (e.g. 30% Off)'),
    ('fixed', 'Dollar Amount Off (e.g. $10 Off)'),
    ('bogo', 'Buy One Get One'),
    ('bundle', 'Bundle Deal (e.g. 2 for $24)'),
    ('price', 'Set Price (e.g. $50 Each)'),
    ('points_multiplier', 'Loyalty Points Multiplier (e.g. 2x Points)'),
    ('clearance', 'Clearance — Near Expiry'),
]


class VendorSubmissionController(http.Controller):

    @http.route(
        '/promos',
        type='http',
        auth='public',
        website=True,
        methods=['GET'],
    )
    def promos_landing(self, month=None, **kw):
        """Vendor entry point.

        - Logged-in vendor (partner linked to >=1 brand): show their promo
          calendar for the selected month.
        - Everyone else: show the market-selection page so they can pick a
          market and submit a deal.
        """
        brands = self._vendor_brands()
        if brands:
            return request.render(
                'mint_command_center.promos_calendar',
                self._calendar_context(brands, month=month),
            )
        return request.render(
            'mint_command_center.promos_market_select',
            self._market_select_context(),
        )

    @http.route(
        '/vendor-deals',
        type='http',
        auth='public',
        website=True,
        methods=['GET', 'POST'],
    )
    def vendor_deal_form(self, **post):
        if request.httprequest.method == 'GET':
            # Pre-select the market when arriving from a /promos market card.
            form_values = {}
            if post.get('market_id'):
                form_values['market_id'] = str(post['market_id'])
            return request.render(
                'mint_command_center.vendor_deal_form',
                self._form_context(form_values=form_values),
            )

        # --- POST: validate and create submission ---
        ctx = self._form_context(form_values=post)

        # Required field validation
        errors = []
        if not post.get('vendor_name', '').strip():
            errors.append('Vendor / Brand Name is required.')
        if not post.get('deal_name', '').strip():
            errors.append('Deal Name is required.')
        if not post.get('vendor_email', '').strip():
            errors.append('Vendor Email is required.')

        if errors:
            ctx['error'] = ' '.join(errors)
            return request.render('mint_command_center.vendor_deal_form', ctx)

        # Build record values
        vals = {
            'vendor_name': post.get('vendor_name', '').strip(),
            'vendor_email': post.get('vendor_email', '').strip(),
            'vendor_contact': post.get('vendor_contact', '').strip(),
            'vendor_phone': post.get('vendor_phone', '').strip(),
            'name': post.get('deal_name', '').strip(),
            'product_category': post.get('product_category', ''),
            'discount_type': post.get('discount_type', ''),
            'details_exclusions': post.get('details_exclusions', '').strip(),
            'sales_details': post.get('sales_details', '').strip(),
            'vendor_funding_terms': post.get('vendor_funding_terms', '').strip(),
        }

        # Numeric fields
        try:
            vals['discount_value'] = float(post.get('discount_value') or 0)
        except (ValueError, TypeError):
            vals['discount_value'] = 0.0

        # Structured BOGO spec (#93677): the public dropdown posts a variant
        # (b1g1/b2g1/b3g1) + a fractional get-discount; map the variant to the
        # canonical buy/get quantities. The flat Discount Value input is only
        # hidden client-side for bogo/bundle — it still POSTs, so zero it here
        # or a value typed before switching the type rides along invisibly.
        if vals['discount_type'] in ('bogo', 'bundle'):
            vals['discount_value'] = 0.0
        if vals['discount_type'] == 'bogo':
            variant = post.get('bogo_variant') or 'b1g1'
            if variant not in BOGO_VARIANT_QTYS:
                variant = 'b1g1'
            buy, get = BOGO_VARIANT_QTYS[variant]
            try:
                get_pct = float(post.get('bogo_get_pct') or 1.0)
            except (ValueError, TypeError):
                get_pct = 1.0
            if get_pct > 1:          # tolerate whole-percent posts (50 = 50%)
                get_pct = get_pct / 100.0
            get_pct = min(max(get_pct, 0.0), 1.0) or 1.0
            vals.update({
                'bogo_variant': variant,
                'bogo_buy_qty': buy,
                'bogo_get_qty': get,
                'bogo_get_pct': get_pct,
            })

        # Structured bundle tiers (#93677): up to 3 qty/price rows posted as
        # bundle_qty_N / bundle_price_N. Rows missing either half are skipped.
        if vals['discount_type'] == 'bundle':
            tiers = []
            for i in (1, 2, 3):
                try:
                    qty = int(float(post.get(f'bundle_qty_{i}') or 0))
                    price = float(post.get(f'bundle_price_{i}') or 0)
                except (ValueError, TypeError):
                    continue
                if qty > 0 and price > 0:
                    tiers.append((0, 0, {'sequence': i * 10, 'qty': qty, 'price': price}))
            if not tiers:
                ctx['error'] = ('Bundle deals need at least one pricing tier — '
                                'fill in a quantity and price (e.g. 2 for $24).')
                return request.render('mint_command_center.vendor_deal_form', ctx)
            vals['bundle_tier_ids'] = tiers

        # Onchange auto-fill doesn't run on RPC create — generate the Sales
        # Details text from the structured spec when the vendor left it blank
        # ("B1G1 50% Off" / "2 for $24 or 3 for $30"). Done in vals (not a
        # post-create write) so the row is complete in one INSERT.
        if not vals['sales_details']:
            generated = ''
            if vals['discount_type'] == 'bogo':
                generated = format_bogo_text(
                    vals['bogo_buy_qty'], vals['bogo_get_qty'],
                    vals['bogo_get_pct'])
            elif vals['discount_type'] == 'bundle':
                generated = format_bundle_tiers_text(
                    [(t[2]['qty'], t[2]['price'])
                     for t in vals['bundle_tier_ids']])
            if generated:
                vals['sales_details'] = generated
                vals['sales_details_autogen'] = generated

        try:
            vals['original_price'] = float(post.get('original_price') or 0)
        except (ValueError, TypeError):
            vals['original_price'] = 0.0

        try:
            vals['vendor_funding_amount'] = float(post.get('vendor_funding_amount') or 0)
        except (ValueError, TypeError):
            vals['vendor_funding_amount'] = 0.0

        try:
            vals['vendor_funding_percent'] = float(post.get('vendor_funding_percent') or 0)
        except (ValueError, TypeError):
            vals['vendor_funding_percent'] = 0.0

        # Brand (mint.brand lookup, with text fallback)
        brand_id_raw = post.get('brand_id', '').strip()
        if brand_id_raw:
            try:
                brand_id_int = int(brand_id_raw)
                if request.env['mint.brand'].sudo().browse(brand_id_int).exists():
                    vals['brand_id'] = brand_id_int
            except (ValueError, TypeError):
                pass

        # Specific products (optional, #93642). The picker posts a comma-separated
        # list of product.template ids. Keep only ids that exist AND belong to the
        # chosen brand — a public form must not let a vendor restrict a deal to
        # another brand's SKUs. Empty list → brand+category fallback downstream.
        product_ids_raw = post.get('product_ids', '') or ''
        picked_ids = []
        for tok in product_ids_raw.replace(',', ' ').split():
            try:
                picked_ids.append(int(tok))
            except (ValueError, TypeError):
                continue
        if picked_ids and vals.get('brand_id'):
            valid = request.env['product.template'].sudo().search([
                ('id', 'in', picked_ids),
                ('brand_id', '=', vals['brand_id']),
            ])
            if valid:
                vals['product_ids'] = [(6, 0, valid.ids)]

        # Market — and default Requested Stores to that market's live
        # dispensaries. The public form has no store picker, so mirror the
        # backend onchange (mint.deal.submission._onchange_market_fill_stores):
        # is_dispensary & is_active, matching mint.region.store_count.
        market_id = post.get('market_id')
        if market_id:
            try:
                region = request.env['mint.region'].sudo().browse(int(market_id))
            except (ValueError, TypeError):
                region = None
            if region and region.exists():
                vals['market_id'] = region.id
                stores = region.store_ids.filtered(
                    lambda c: getattr(c, 'is_dispensary', False)
                    and getattr(c, 'is_active', True)
                )
                if stores:
                    vals['store_ids'] = [(6, 0, stores.ids)]

        # Dates
        if post.get('preferred_start_date'):
            vals['preferred_start_date'] = post['preferred_start_date']
        if post.get('preferred_end_date'):
            vals['preferred_end_date'] = post['preferred_end_date']
        if post.get('preferred_days'):
            vals['preferred_days'] = post['preferred_days'].strip()

        try:
            env = request.env
            submission = env['mint.deal.submission'].sudo().create(vals)

            # Mirror the submission as a CRM opportunity on the Vendor Promos
            # pipeline so the marketing lead can work it from CRM (replies from
            # the vendor get threaded onto the lead via the catchall alias).
            try:
                lead_vals = {
                    'name': f"Promo - {vals['vendor_name']}: {vals['name']}",
                    'type': 'opportunity',
                    'contact_name': vals.get('vendor_contact') or vals['vendor_name'],
                    'partner_name': vals['vendor_name'],
                    'email_from': vals.get('vendor_email') or False,
                    'phone': vals.get('vendor_phone') or False,
                    'vendor_brand_id': vals.get('brand_id') or False,
                    'vendor_funding_amount': vals.get('vendor_funding_amount') or 0.0,
                    'vendor_funding_percent': vals.get('vendor_funding_percent') or 0.0,
                    'description': vals.get('sales_details') or vals.get('details_exclusions') or '',
                }
                team = env.ref(
                    'sales_team.salesteam_vendor_promos', raise_if_not_found=False,
                ) or env['crm.team'].sudo().search(
                    [('name', '=', 'Vendor Promos')], limit=1,
                )
                if team:
                    lead_vals['team_id'] = team.id
                    if team.user_id:
                        lead_vals['user_id'] = team.user_id.id
                lead = env['crm.lead'].sudo().create(lead_vals)
                submission.sudo().write({'crm_lead_id': lead.id})
            except Exception as lead_err:
                # Submission already saved; don't fail the vendor's request
                # just because the CRM mirror had a hiccup.
                _logger.warning(
                    'Submission %s saved but CRM lead creation failed: %s',
                    submission.id, lead_err,
                )

            ctx['success'] = (
                f"Thank you! Your deal submission has been received "
                f"(Reference: SUB-{submission.id:05d}). "
                f"Our team will review it and get back to you."
            )
            ctx['form_values'] = {}
        except Exception as e:
            _logger.error('Deal submission creation failed: %s', e)
            ctx['error'] = 'An error occurred while submitting your deal. Please try again.'

        return request.render('mint_command_center.vendor_deal_form', ctx)

    @http.route(
        '/promos/brand-products',
        type='http',
        auth='public',
        website=True,
        methods=['GET'],
        sitemap=False,
    )
    def vendor_brand_products(self, brand_id=None, q=None, **kw):
        """Public JSON list of a brand's products, for the deal-form picker.

        Returns only id / name / code (no pricing or internal data). Used by the
        inline script on /promos to populate the optional "Specific Products"
        multi-select once a vendor selects their brand (#93642 phase 5).

        #94167: optional `q` filters by name OR SKU within the brand so brands
        with >500 products are reachable via search instead of silent
        truncation. The body stays a JSON array (back-compat with the existing
        picker JS); the brand-wide total is returned in the X-Total-Count
        header so the UI can show a "showing N of M" hint.
        """
        products = []
        total = 0
        try:
            bid = int(brand_id)
        except (ValueError, TypeError):
            bid = 0
        if bid:
            Product = request.env['product.template'].sudo()
            domain = [('brand_id', '=', bid)]
            term = (q or '').strip()
            if term:
                domain += ['|', ('name', 'ilike', term), ('default_code', 'ilike', term)]
            total = Product.search_count(domain)
            # Bounded payload: a search returns a small page; the no-search
            # initial load keeps the legacy 500 so the picker isn't empty.
            limit = 50 if term else 500
            recs = Product.search_read(
                domain,
                ['id', 'name', 'default_code'],
                order='name',
                limit=limit,
            )
            products = [
                {'id': r['id'], 'name': r['name'], 'code': r['default_code'] or ''}
                for r in recs
            ]
        return request.make_response(
            json.dumps(products),
            headers=[
                ('Content-Type', 'application/json'),
                ('X-Total-Count', str(total)),
            ],
        )

    def _form_context(self, **extra):
        markets = request.env['mint.region'].sudo().search([], order='name')
        # Brand catalog for autocomplete — read-only, public-safe
        brands = request.env['mint.brand'].sudo().search_read(
            [], ['id', 'name'], order='name',
        )
        currency = request.env.company.currency_id
        ctx = {
            'markets': markets,
            'brands': brands,
            'currency_symbol': currency.symbol if currency else '$',
            'categories': PRODUCT_CATEGORIES,
            'discount_types': DISCOUNT_TYPES,
            'error': None,
            'success': None,
            'form_values': {},
        }
        ctx.update(extra)
        return ctx

    # ------------------------------------------------------------------
    # /promos landing helpers
    # ------------------------------------------------------------------

    def _vendor_brands(self):
        """Brands the current logged-in user is a vendor contact for.

        Returns an empty recordset for the public user (not logged in) or for
        any logged-in user whose partner is not linked to a brand.
        """
        user = request.env.user
        if user._is_public():
            return request.env['mint.brand'].sudo().browse()
        return request.env['mint.brand'].sudo().search([
            ('vendor_partner_ids', 'in', user.partner_id.ids),
        ])

    def _market_select_context(self):
        markets = request.env['mint.region'].sudo().search([], order='name')
        return {'markets': markets}

    def _parse_month(self, month):
        """Parse a 'YYYY-MM' string to the first of that month.

        Falls back to the current month on missing/invalid input.
        """
        today = fields.Date.context_today(request.env.user)
        if month:
            try:
                year_s, mon_s = month.split('-')
                return date(int(year_s), int(mon_s), 1)
            except (ValueError, AttributeError):
                pass
        return today.replace(day=1)

    def _calendar_context(self, brands, month=None):
        first = self._parse_month(month)
        today = fields.Date.context_today(request.env.user)

        # Prev / next month anchors for nav.
        prev_month = (first - _ONE_DAY).replace(day=1)
        if first.month == 12:
            next_month = date(first.year + 1, 1, 1)
        else:
            next_month = date(first.year, first.month + 1, 1)
        last_day = _calendar.monthrange(first.year, first.month)[1]
        month_end = date(first.year, first.month, last_day)

        entries = request.env['mint.brand.calendar.entry'].sudo().search([
            ('brand_id', 'in', brands.ids),
            ('date', '>=', first),
            ('date', '<=', month_end),
        ], order='date')

        # Bucket entries by day for O(1) lookup in the template.
        empty = request.env['mint.brand.calendar.entry']
        by_day = {}
        for e in entries:
            by_day[e.date] = by_day.get(e.date, empty) | e

        # Monday-first month grid of weeks -> day cells.
        cal = _calendar.Calendar(firstweekday=0)
        weeks = []
        for week in cal.monthdatescalendar(first.year, first.month):
            cells = []
            for day in week:
                cells.append({
                    'date': day,
                    'day': day.day,
                    'in_month': day.month == first.month,
                    'is_today': day == today,
                    'entries': by_day.get(day, empty),
                })
            weeks.append(cells)

        return {
            'brands': brands,
            'brand_names': ', '.join(brands.mapped('name')),
            'month_label': first.strftime('%B %Y'),
            'weekday_labels': ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'],
            'weeks': weeks,
            'entry_count': len(entries),
            'prev_month': prev_month.strftime('%Y-%m'),
            'next_month': next_month.strftime('%Y-%m'),
        }
