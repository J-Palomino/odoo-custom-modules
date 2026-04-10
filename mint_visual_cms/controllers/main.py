# -*- coding: utf-8 -*-
import json
import logging
import re

from odoo import http
from odoo.http import request

from odoo.addons.mint_banner.models.banner import FRONTEND_URL_PARAM, FRONTEND_URL_DEFAULT

_logger = logging.getLogger(__name__)

# ── Zone definitions mirroring src/pages/[region]/[locale].astro ─────────
# Each zone maps to a visual block in the wireframe.
# 'slot' links to mint.banner slot selection values.

ZONES = [
    {
        'id': 'hero-image',
        'label': 'Store Hero Image',
        'slot': None,
        'editable': False,
        'type': 'info',
        'dimensions': '1600 x 600 px',
        'description': 'Main store background image — edit in company settings',
    },
    {
        'id': 'hero',
        'label': 'Hero Carousel',
        'slot': 'hero',
        'editable': True,
        'type': 'banner',
        'dimensions': '1600 x 400 px (4:1)',
        'description': 'Full-width rotating banner carousel',
    },
    {
        'id': 'brand-spotlight',
        'label': 'Brand Spotlight',
        'slot': 'brand-spotlight',
        'editable': True,
        'type': 'banner',
        'dimensions': '1200 x 300 px',
        'description': 'Featured brand banners below hero',
    },
    {
        'id': 'best-deals',
        'label': 'Best Deals',
        'slot': None,
        'editable': False,
        'type': 'placeholder',
        'dimensions': '',
        'description': 'Auto-populated from inventory — top discounted products',
    },
    {
        'id': 'shop-by-category',
        'label': 'Shop by Category',
        'slot': None,
        'editable': False,
        'type': 'placeholder',
        'dimensions': '',
        'description': 'Auto-populated category grid',
    },
    {
        'id': 'after-flower',
        'label': 'Flower Section',
        'slot': 'after-flower',
        'editable': True,
        'type': 'banner',
        'dimensions': '1200 x 300 px',
        'description': 'Ad banner above Flower products',
    },
    {
        'id': 'after-pre-rolls',
        'label': 'Pre-Rolls Section',
        'slot': 'after-pre-rolls',
        'editable': True,
        'type': 'banner',
        'dimensions': '1200 x 300 px',
        'description': 'Ad banner above Pre-Rolls products',
    },
    {
        'id': 'after-vapes',
        'label': 'Vapes Section',
        'slot': 'after-vapes',
        'editable': True,
        'type': 'banner',
        'dimensions': '1200 x 300 px',
        'description': 'Ad banner above Vapes products',
    },
    {
        'id': 'after-edibles',
        'label': 'Edibles Section',
        'slot': 'after-edibles',
        'editable': True,
        'type': 'banner',
        'dimensions': '1200 x 300 px',
        'description': 'Ad banner above Edibles products',
    },
    {
        'id': 'after-concentrates',
        'label': 'Concentrates Section',
        'slot': 'after-concentrates',
        'editable': True,
        'type': 'banner',
        'dimensions': '1200 x 300 px',
        'description': 'Ad banner above Concentrates products',
    },
    {
        'id': 'gallery',
        'label': 'Photo Gallery',
        'slot': None,
        'editable': False,
        'type': 'info',
        'dimensions': '400 x 400 px per photo',
        'description': 'Store photo gallery — edit in company settings',
    },
    {
        'id': 'deals-popup',
        'label': 'Deals Popup',
        'slot': 'deals-popup',
        'editable': True,
        'type': 'banner',
        'dimensions': '800 x 600 px',
        'description': 'Bottom-right popup overlay on all pages',
    },
]


# ── Helpers ──────────────────────────────────────────────────────────────

def _get_frontend_base_url():
    icp = request.env['ir.config_parameter'].sudo()
    return (icp.get_param(FRONTEND_URL_PARAM) or FRONTEND_URL_DEFAULT).rstrip('/')


def _get_stores():
    """Return list of store dicts with id, name, slug, region, state.

    Sorted by (state, name) so consumers can group consecutively by state.
    """
    companies = request.env['res.company'].sudo().search(
        [('parent_id', '!=', False)],
    )
    stores = []
    for c in companies:
        slug = c.x_slug if hasattr(c, 'x_slug') and c.x_slug else False
        if not slug:
            continue
        state_name = c.state_id.name if c.state_id else ''
        region = re.sub(r'\s*\([^)]*\)\s*$', '', state_name).strip().lower().replace(' ', '-')
        stores.append({
            'id': c.id,
            'name': c.name,
            'slug': slug,
            'region': region or 'locations',
            'state': state_name,
        })
    stores.sort(key=lambda s: (s['state'] or '\uffff', s['name']))
    return stores


def _get_company_by_slug(slug):
    """Return res.company record for a given slug, or False."""
    return request.env['res.company'].sudo().search(
        [('parent_id', '!=', False), ('x_slug', '=', slug)], limit=1,
    )


def _get_zone_banner_counts(company_id):
    """Return dict of slot → {total, published, scheduled} counts for a company."""
    Banner = request.env['mint.banner'].sudo()
    counts = {}
    for zone in ZONES:
        slot = zone.get('slot')
        if not slot:
            continue
        domain = [('slot', '=', slot), ('active', '=', True)]
        if company_id:
            domain.append(('company_id', 'in', [company_id, False]))
        banners = Banner.search(domain)
        counts[slot] = {
            'total': len(banners),
            'published': len(banners.filtered(lambda b: b.publish_status == 'published')),
            'scheduled': len(banners.filtered(lambda b: b.publish_status == 'scheduled')),
        }
    return counts


def _get_store_banner_summary():
    """Return dict of company_id → total active banner count (for landing grid)."""
    Banner = request.env['mint.banner'].sudo()
    all_banners = Banner.search([('active', '=', True)])
    summary = {}
    for b in all_banners:
        cid = b.company_id.id if b.company_id else 'global'
        summary[cid] = summary.get(cid, 0) + 1
    return summary


def _banner_to_dict(b):
    """Serialize a mint.banner record for JSON responses."""
    image_src = b.image_url or ''
    if not image_src and b.image:
        # Build Odoo image URL for binary field
        image_src = f'/web/image/mint.banner/{b.id}/image'
    return {
        'id': b.id,
        'name': b.name,
        'title': b.title or '',
        'subtitle': b.subtitle or '',
        'slot': b.slot,
        'size': b.size,
        'image_url': image_src,
        'has_image': bool(b.image or b.image_url),
        'status': b.publish_status,
        'sequence': b.sequence,
        'company_id': b.company_id.id if b.company_id else False,
        'company_name': b.company_id.name if b.company_id else 'Global',
        'date_start': str(b.date_start) if b.date_start else '',
        'date_end': str(b.date_end) if b.date_end else '',
        'link_url': b.link_url or '',
        'brand': b.brand or '',
    }


# ── Controller ───────────────────────────────────────────────────────────

class VisualCMSController(http.Controller):

    # ─── Pages ───────────────────────────────────────────────────────

    @http.route('/visual-cms', type='http', auth='user', website=True)
    def landing_page(self, **kwargs):
        stores = _get_stores()
        banner_summary = _get_store_banner_summary()
        global_count = banner_summary.pop('global', 0)

        stores_grouped = []
        current_state = object()
        for s in stores:
            state = s.get('state') or 'Unknown State'
            if state != current_state:
                stores_grouped.append({'state': state, 'stores': []})
                current_state = state
            stores_grouped[-1]['stores'].append(s)

        return request.render('mint_visual_cms.landing_page', {
            'stores': stores,
            'stores_grouped': stores_grouped,
            'banner_summary': banner_summary,
            'global_count': global_count,
        })

    @http.route('/visual-cms/<string:slug>', type='http', auth='user', website=True)
    def wireframe_page(self, slug, **kwargs):
        stores = _get_stores()
        store = next((s for s in stores if s['slug'] == slug), None)
        if not store:
            return request.not_found()

        company = _get_company_by_slug(slug)
        company_id = company.id if company else False
        zone_counts = _get_zone_banner_counts(company_id)

        base_url = _get_frontend_base_url()
        preview_url = f"{base_url}/{store['region']}/{store['slug']}"

        vcms_json = json.dumps({
            'companyId': company_id or False,
            'storeSlug': store['slug'],
            'storeName': store['name'],
        })

        return request.render('mint_visual_cms.wireframe_page', {
            'store': store,
            'stores': stores,
            'zones': ZONES,
            'zone_counts': zone_counts,
            'company_id': company_id,
            'preview_url': preview_url,
            'vcms_json': vcms_json,
        })

    # ─── JSON API ────────────────────────────────────────────────────

    @http.route('/visual-cms/api/slot-banners', type='json', auth='user')
    def api_slot_banners(self, slot, company_id=None, **kwargs):
        domain = [('slot', '=', slot), ('active', '=', True)]
        if company_id:
            domain.append(('company_id', 'in', [int(company_id), False]))
        banners = request.env['mint.banner'].sudo().search(
            domain, order='sequence, id',
        )
        return [_banner_to_dict(b) for b in banners]

    @http.route('/visual-cms/api/reorder', type='json', auth='user')
    def api_reorder(self, banner_ids, **kwargs):
        Banner = request.env['mint.banner'].sudo()
        for seq, bid in enumerate(banner_ids):
            Banner.browse(int(bid)).write({'sequence': seq * 10})
        return {'status': 'ok'}

    @http.route('/visual-cms/api/clear-cache', type='json', auth='user')
    def api_clear_cache(self, **kwargs):
        try:
            request.env['mint.banner'].sudo().action_clear_frontend_cache()
            return {'status': 'ok', 'message': 'Frontend cache cleared'}
        except Exception as e:
            _logger.warning('Visual CMS cache clear failed: %s', e)
            return {'status': 'error', 'message': str(e)}
