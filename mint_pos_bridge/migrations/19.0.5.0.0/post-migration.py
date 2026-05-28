# -*- coding: utf-8 -*-
"""
Post-migration for mint_pos_bridge 19.0.5.0.0.

Phase 1 of the per-store swimlane mirror initiative
(docs/per-store-swimlane-mirror-implementation-plan.md).

Goal:
  Seed mint.pos.lane records so the new lane_id field is non-empty for every
  live order. After this migration:
    - Every store with a mint.web.order.config has at least one lane per
      state_lane_map entry.
    - Specific known custom lanes (Employee, ONLINE READY, FILLED PICKUP
      ORDERS, etc.) discovered in the 2026-05-22 audit are pre-created.
    - Every mint.pos.order in a non-terminal state has lane_id set.

The bootstrap is intentionally additive — LaneWatcher (Phase 2) auto-creates
any lane Dutchie surfaces that we haven't seeded here. Idempotent: the
(company_id, dutchie_lane_name) unique constraint guards re-runs.
"""

import logging

from odoo.addons.mint_pos_bridge.models.pos_lane import (
    STATE_TO_CATEGORY,
    VALID_CATEGORIES,
)

_logger = logging.getLogger(__name__)


# Lane category mappings — keyed by lowercased dutchie_lane_name.
# Maps the verbatim Dutchie TransactionStatus to a category from
# mint.pos.lane.LANE_CATEGORIES. Falls through to category=NULL when unknown
# (surfaces to the ops triage view).
_LANE_NAME_TO_CATEGORY = {
    'lobby': 'lobby',
    'online orders': 'online_orders',
    'online order': 'online_orders',
    'online ready': 'online_orders',
    'filled pickup orders': 'pickup',
    'sales floor': 'sales_floor',
    'sales floor / needs code': 'sales_floor',
    'sales floor/ needs code': 'sales_floor',
    'processing': 'processing',
    'pick-up': 'pickup',
    'pickup': 'pickup',
    'deli counter': 'deli_counter',
    'credit checkout': 'credit_checkout',
    'delivery': 'delivery',
    'ready for delivery': 'ready_delivery',
    'delivery in progess': 'delivery_progress',
    'delivery in progress': 'delivery_progress',
    'delivery completed': 'delivery_progress',  # category for live (non-terminal)
    'employee': 'employee',
    'employee purchases': 'employee',
    'employee order': 'employee',
    'employee / test': 'employee',
    'unassigned': 'unassigned',
}

# Known store-specific lanes from the 2026-05-22 swimlane discovery audit.
# Keyed by company.x_slug — created in addition to whatever state_lane_map
# provides. LaneWatcher will keep the list current going forward.
_STORE_SPECIFIC_LANES = {
    'mesa':              ['Employee'],
    '75th-ave':          ['Employee', 'EMPLOYEE PURCHASES'],
    'bell-road':         ['Employee'],
    'scottsdale':        ['Employee', 'ONLINE READY'],
    'northern-phoenix':  ['FILLED PICKUP ORDERS'],
    'power':             ['Unassigned'],
}

# Live (non-terminal) order states drive the backfill. These are exactly the
# keys of STATE_TO_CATEGORY (terminal states are absent → those orders get no
# lane). Each maps to a valid lane category.
_LIVE_STATES = tuple(STATE_TO_CATEGORY.keys())


def _categorize(dutchie_name):
    cat = _LANE_NAME_TO_CATEGORY.get((dutchie_name or '').strip().lower())
    return cat if cat in VALID_CATEGORIES else None


def _seed_lanes_for_company(env, company):
    """Seed standard + store-specific lanes for one company."""
    Lane = env['mint.pos.lane'].sudo()
    config = env['mint.web.order.config'].sudo().search(
        [('company_id', '=', company.id), ('active', '=', True)],
        limit=1,
    )
    seeded = 0
    seen_names = set()

    # 1. Seed from web_order_config.state_lane_map (the explicit per-store config).
    if config:
        import json
        try:
            state_map = json.loads(config.state_lane_map or '{}')
        except (json.JSONDecodeError, TypeError):
            state_map = {}
        seq = 10
        for state_key, lane_name in state_map.items():
            lane_name = (lane_name or '').strip()
            if not lane_name or lane_name in seen_names:
                continue
            seen_names.add(lane_name)
            # Prefer the Odoo state_key → category mapping; fall back to
            # categorizing the verbatim lane name. Both yield a valid category
            # or None (uncategorized → ops triage).
            cat = STATE_TO_CATEGORY.get(state_key) or _categorize(lane_name)
            try:
                Lane.create({
                    'company_id': company.id,
                    'dutchie_lane_name': lane_name,
                    'name': lane_name,
                    'category': cat,
                    'sequence': seq,
                })
                seeded += 1
            except Exception as exc:  # IntegrityError on re-run — ignore
                _logger.debug('  lane %s/%s already exists: %s', company.id, lane_name, exc)
            seq += 10

    # 2. Layer in known store-specific lanes from the discovery audit.
    extras = _STORE_SPECIFIC_LANES.get(company.x_slug or '', [])
    seq = 200
    for lane_name in extras:
        if lane_name in seen_names:
            continue
        seen_names.add(lane_name)
        try:
            Lane.create({
                'company_id': company.id,
                'dutchie_lane_name': lane_name,
                'name': lane_name,
                'category': _categorize(lane_name),
                'sequence': seq,
            })
            seeded += 1
        except Exception as exc:
            _logger.debug('  extra lane %s/%s already exists: %s', company.id, lane_name, exc)
        seq += 10

    return seeded


def _backfill_lane_id(env):
    """Set lane_id on every live (non-terminal) mint.pos.order."""
    Lane = env['mint.pos.lane'].sudo()
    PosOrder = env['mint.pos.order'].sudo()
    updated = 0
    orders = PosOrder.search([
        ('lane_id', '=', False),
        ('state', 'in', list(_LIVE_STATES)),
    ])
    _logger.info('  backfilling lane_id on %d live orders', len(orders))

    # Cache lanes per (company_id, category) so we don't re-search per order.
    lane_cache = {}
    for order in orders:
        cid = order.company_id.id
        # Map the order's state to a valid lane category. Skip if unmappable
        # (shouldn't happen — search domain already filters to live states).
        cat = STATE_TO_CATEGORY.get(order.state)
        if not cat or not cid:
            continue
        key = (cid, cat)
        if key not in lane_cache:
            lane = Lane.search(
                [('company_id', '=', cid), ('category', '=', cat), ('active', '=', True)],
                order='sequence, id',
                limit=1,
            )
            if not lane:
                # No seeded lane matches — create one so the order stays visible.
                lane = Lane.find_or_create_by_dutchie_name(
                    cid, cat.replace('_', ' ').title(),
                )
                if not lane.category:
                    lane.category = cat
            lane_cache[key] = lane.id
        # Bypass write() to avoid retriggering the synchronizer.
        order.with_context(from_dutchie_sync=True).write({'lane_id': lane_cache[key]})
        updated += 1
    return updated


def migrate(cr, version):
    _logger.info(
        'mint_pos_bridge 19.0.5.0.0 post-migration: seeding mint.pos.lane + '
        'backfilling mint.pos.order.lane_id'
    )

    from odoo import api, SUPERUSER_ID
    env = api.Environment(cr, SUPERUSER_ID, {})

    # 1. Seed lanes for every company that has a web_order_config (the set of
    #    companies that route to a Dutchie POS).
    companies = env['res.company'].sudo().search([])
    seeded_total = 0
    for company in companies:
        # Only seed when the company has an active web_order_config OR is
        # listed in store-specific lanes — skip non-dispensary companies.
        has_config = env['mint.web.order.config'].sudo().search_count(
            [('company_id', '=', company.id), ('active', '=', True)],
        )
        in_audit = (company.x_slug or '') in _STORE_SPECIFIC_LANES
        if not has_config and not in_audit:
            continue
        n = _seed_lanes_for_company(env, company)
        seeded_total += n
        _logger.info(
            '  seeded %d lanes for company id=%s slug=%s',
            n, company.id, company.x_slug or '(none)',
        )
    _logger.info('  total lanes seeded: %d', seeded_total)

    # 2. Backfill lane_id on every live order.
    backfilled = _backfill_lane_id(env)
    _logger.info('  backfilled lane_id on %d orders', backfilled)

    _logger.info('mint_pos_bridge 19.0.5.0.0 post-migration complete')
