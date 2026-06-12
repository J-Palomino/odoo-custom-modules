import json
import logging
from datetime import datetime

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


def _parse_id_list(raw):
    """Parse a multi-value POST key into a list of valid int IDs.

    Accepts either a list of strings (when the form posts the field multiple
    times) or a single comma-separated string (when an HTML widget joins them
    client-side). Filters non-integer entries silently.
    """
    if not raw:
        return []
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.split(',') if p.strip()]
    else:
        parts = []
        for item in raw:
            if isinstance(item, str):
                parts.extend(p.strip() for p in item.split(',') if p.strip())
    out = []
    for p in parts:
        try:
            out.append(int(p))
        except (ValueError, TypeError):
            continue
    return out

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
        ['/promos', '/vendor-deals'],
        type='http',
        auth='public',
        website=True,
        methods=['GET', 'POST'],
    )
    def vendor_deal_form(self, **post):
        if request.httprequest.method == 'GET':
            return request.render(
                'mint_command_center.vendor_deal_form',
                self._form_context(),
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

        # Conditional-required: Event Name when is_holiday is set (#93648).
        # Server-side guard backing the model's @api.constrains; surfaces
        # earlier in the form re-render than the ORM exception would.
        is_holiday_raw = post.get('is_holiday', '')
        is_holiday = str(is_holiday_raw).lower() in ('1', 'true', 'on', 'yes')
        if is_holiday and not post.get('event_name', '').strip():
            errors.append('Event Name is required when Special Event / Holiday is enabled.')

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
            'inclusions': post.get('inclusions', '').strip(),
            'details_exclusions': post.get('details_exclusions', '').strip(),
            'sales_details': post.get('sales_details', '').strip(),
            'vendor_funding_terms': post.get('vendor_funding_terms', '').strip(),
            'is_holiday': is_holiday,
            'event_name': post.get('event_name', '').strip(),
            'promo_units_enabled': str(
                post.get('promo_units_enabled', '')
            ).lower() in ('1', 'true', 'on', 'yes'),
            'promo_units_product': post.get('promo_units_product', '').strip(),
        }

        # Numeric fields
        try:
            vals['discount_value'] = float(post.get('discount_value') or 0)
        except (ValueError, TypeError):
            vals['discount_value'] = 0.0

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

        try:
            vals['weight_value'] = float(post.get('weight_value') or 0)
        except (ValueError, TypeError):
            vals['weight_value'] = 0.0
        weight_unit = post.get('weight_unit', '').strip()
        if weight_unit in ('g', 'mg', 'oz', 'ct'):
            vals['weight_unit'] = weight_unit

        try:
            vals['promo_units_quantity'] = int(post.get('promo_units_quantity') or 0)
        except (ValueError, TypeError):
            vals['promo_units_quantity'] = 0

        # Brand (mint.brand lookup, with text fallback)
        brand_id_raw = post.get('brand_id', '').strip()
        if brand_id_raw:
            try:
                brand_id_int = int(brand_id_raw)
                if request.env['mint.brand'].sudo().browse(brand_id_int).exists():
                    vals['brand_id'] = brand_id_int
            except (ValueError, TypeError):
                pass

        # Markets — multi-select replaces the single market_id picker on the
        # public form. market_id is now a stored compute = market_ids[:1] so
        # downstream readers stay compatible.
        market_ids_raw = (
            request.httprequest.form.getlist('market_ids')
            if hasattr(request.httprequest, 'form')
            else post.get('market_ids')
        )
        market_ids = _parse_id_list(market_ids_raw)
        if not market_ids and post.get('market_id'):
            # Legacy single-market POST shape — keep working.
            try:
                market_ids = [int(post['market_id'])]
            except (ValueError, TypeError):
                market_ids = []
        if market_ids:
            valid = request.env['mint.region'].sudo().browse(market_ids).exists().ids
            if valid:
                vals['market_ids'] = [(6, 0, valid)]

        # Stores — multi-select (the locale picker on the public form).
        # Render filtered by chosen markets in the template; server validates ids.
        store_ids_raw = (
            request.httprequest.form.getlist('store_ids')
            if hasattr(request.httprequest, 'form')
            else post.get('store_ids')
        )
        store_ids = _parse_id_list(store_ids_raw)
        if store_ids:
            valid = request.env['res.company'].sudo().browse(store_ids).exists().ids
            if valid:
                vals['store_ids'] = [(6, 0, valid)]

        # Products — multi-select filtered by brand_id ∩ product_category.
        product_ids_raw = (
            request.httprequest.form.getlist('product_ids')
            if hasattr(request.httprequest, 'form')
            else post.get('product_ids')
        )
        product_ids = _parse_id_list(product_ids_raw)
        if product_ids:
            # Security (#93642): a public form must not let a vendor restrict a
            # deal to another brand's SKUs — scope to the chosen brand.
            domain = [('id', 'in', product_ids)]
            if vals.get('brand_id'):
                domain.append(('brand_id', '=', vals['brand_id']))
            valid = request.env['product.template'].sudo().search(domain).ids
            if valid:
                vals['product_ids'] = [(6, 0, valid)]

        # Dates
        if post.get('preferred_start_date'):
            vals['preferred_start_date'] = post['preferred_start_date']
        if post.get('preferred_end_date'):
            vals['preferred_end_date'] = post['preferred_end_date']
        if post.get('preferred_days'):
            vals['preferred_days'] = post['preferred_days'].strip()

        # Promo Units delivery date — only meaningful when promo_units_enabled
        if vals.get('promo_units_enabled') and post.get('promo_units_delivery_date'):
            vals['promo_units_delivery_date'] = post['promo_units_delivery_date']

        # Plot Dates — multi-date picker on the public form sends one
        # plot_dates entry per checked day (YYYY-MM-DD). Build an o2m create
        # command for mint.deal.submission.day.
        plot_dates_raw = (
            request.httprequest.form.getlist('plot_dates')
            if hasattr(request.httprequest, 'form')
            else post.get('plot_dates')
        )
        if isinstance(plot_dates_raw, str):
            plot_dates_raw = [d.strip() for d in plot_dates_raw.split(',') if d.strip()]
        elif not plot_dates_raw:
            plot_dates_raw = []
        plot_day_creates = []
        for date_str in plot_dates_raw:
            try:
                datetime.strptime(date_str, '%Y-%m-%d')
            except (ValueError, TypeError):
                continue
            plot_day_creates.append((0, 0, {'date': date_str}))
        if plot_day_creates:
            vals['plot_date_ids'] = plot_day_creates

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
        # Stores per market — fuels the locale (store) multi-select that
        # narrows on chosen Markets. Distribution Hub is excluded (#93666):
        # filter by name to keep the public surface honest even without a
        # dedicated flag on res.company.
        Store = request.env['res.company'].sudo()
        store_domain = [
            ('id', 'in', markets.mapped('store_ids').ids),
            ('name', 'not ilike', 'Distribution Hub'),
        ]
        stores = Store.search_read(store_domain, ['id', 'name'], order='name')
        # Map of market_id → [store_id, ...] for client-side narrowing.
        stores_by_market = {
            market.id: [s.id for s in market.store_ids
                        if 'Distribution Hub' not in (s.name or '')]
            for market in markets
        }
        currency = request.env.company.currency_id
        ctx = {
            'markets': markets,
            'brands': brands,
            'stores': stores,
            'stores_by_market': stores_by_market,
            'currency_symbol': currency.symbol if currency else '$',
            'categories': PRODUCT_CATEGORIES,
            'discount_types': DISCOUNT_TYPES,
            'weight_units': [('g', 'g'), ('mg', 'mg'), ('oz', 'oz'), ('ct', 'ct')],
            'error': None,
            'success': None,
            'form_values': {},
        }
        ctx.update(extra)
        return ctx
