import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)

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

        # Market
        market_id = post.get('market_id')
        if market_id:
            try:
                vals['market_id'] = int(market_id)
            except (ValueError, TypeError):
                pass

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
