"""Stage 2 (#2) migration: seed the shared deal-keyed Dutchie registry.

Path-1 published consolidated discounts keyed by the SUBMISSION ExternalId
(lgm_<sub> / lgm_<sub>_w<k>), recorded in mint.deal.submission.
dutchie_publish_loc_ids. Stage 2 re-keys to the unified, deal-keyed ExternalId
(lgm_deal_<deal_id>[_w<k>]) stored on mint.ptl.deal.dutchie_publish_ids, shared
by both publish paths.

Seeding the deal registry with the EXISTING Dutchie id under the NEW key means
the first unified publish UPDATES the live record in place (re-keying its
ExternalId) instead of CREATING a duplicate. Without this seed, the cutover
would dup every already-published path-1 deal.

Path-2 (per-store) reconciliation is handled separately in the path-2
conversion — its per-store records can't map 1:1 to a single consolidated id.
Idempotent: only fills keys not already present; safe to re-run.
"""
import json
import logging

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)


def _new_key(old_key, sub_id, deal_id):
    base = "lgm_%d" % sub_id
    if old_key == base:
        return "lgm_deal_%d" % deal_id
    if old_key.startswith(base + "_w"):
        return "lgm_deal_%d%s" % (deal_id, old_key[len(base):])  # carries _w<k>
    return None


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    subs = env['mint.deal.submission'].search([
        ('deal_id', '!=', False),
        ('dutchie_publish_loc_ids', '!=', False),
    ])
    seeded = 0
    for sub in subs:
        deal = sub.deal_id
        if not deal:
            continue
        try:
            old_map = json.loads(sub.dutchie_publish_loc_ids or '{}')
        except (ValueError, TypeError):
            continue
        reg = deal._dutchie_registry()
        changed = False
        for old_key, val in old_map.items():
            # Only consolidated int ids carry over (legacy flat/nested per-store
            # maps don't correspond to a single consolidated record).
            if not (isinstance(val, int) and not isinstance(val, bool) and val > 0):
                continue
            nk = _new_key(old_key, sub.id, deal.id)
            if nk and nk not in reg:
                reg[nk] = val
                changed = True
        if changed:
            deal.sudo().write({'dutchie_publish_ids': json.dumps(reg)})
            seeded += 1
    _logger.info("Stage 2 migration: seeded dutchie_publish_ids on %d deal(s) "
                 "from %d submission map(s)", seeded, len(subs))
