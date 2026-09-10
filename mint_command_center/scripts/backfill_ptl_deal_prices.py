#!/usr/bin/env python3
"""Backfill structured prices onto PTL deals whose price lives only in their name.

Why this exists
---------------
A wave of mint.ptl.deal records was created with the price written ONLY into the
display name ("$18 -> $10.80 40% Off Potpots", "2 for $50 Mac Pharms") and every
structured field left empty: discount_value=0, no bogo quantities, no bundle
tiers. _deal_to_discount_vals then faithfully forwards that emptiness, so the
published mint.discount gets discount_amount=0 and calculation_method_id=0 --
the storefront renders an on-sale badge with no actual price cut.

This script recovers the numbers from the names and writes them back onto the
DEAL (the root cause), not onto the discount: _ensure_discount() rewrites the
discount from the deal on every publish, so a discount-only patch is clobbered
on the next run.

Safety
------
* Dry-run by default. Nothing is written without --apply.
* --apply first writes a rollback JSON containing every field's prior value.
* Only deals whose declared discount_type AGREES with the parsed name shape are
  touched. Deals with no discount_type, and BOGOs (whose buy/get quantities are
  not recoverable from the name), are reported and skipped.

Usage
-----
    python3 backfill_ptl_deal_prices.py                 # dry run
    python3 backfill_ptl_deal_prices.py --apply         # write + rollback file
    python3 backfill_ptl_deal_prices.py --rollback FILE   # undo an --apply
"""
import argparse
import json
import os
import re
import sys
import xmlrpc.client
from datetime import datetime, timezone

ARROW = re.compile(r'\$\s*([\d,]+(?:\.\d+)?)\s*(?:→|->|to)\s*\$\s*([\d,]+(?:\.\d+)?)', re.I)
NFOR = re.compile(r'(\d+)\s*(?:for|/)\s*\$\s*([\d,]+(?:\.\d+)?)', re.I)
PCT = re.compile(r'(\d+(?:\.\d+)?)\s*%\s*off', re.I)

# Fields captured for rollback before any write.
TRACKED = ['discount_value', 'original_price']


def money(s):
    return float(s.replace(',', ''))


def connect(env_path):
    env = {}
    with open(os.path.expanduser(env_path)) as fh:
        for line in fh:
            m = re.match(r'^\s*([A-Z_]+)\s*=\s*(.*)\s*$', line)
            if m:
                env[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    url = env.get('ODOO_URL', 'https://letsgomint.us')
    db = env.get('ODOO_DB', 'odoo')
    user = env.get('ODOO_USERNAME')
    key = env.get('ODOO_API_KEY')
    common = xmlrpc.client.ServerProxy('%s/xmlrpc/2/common' % url)
    uid = common.authenticate(db, user, key, {})
    if not uid:
        sys.exit('Odoo authentication failed')
    models = xmlrpc.client.ServerProxy('%s/xmlrpc/2/object' % url)

    def rpc(model, method, args, kw=None):
        return models.execute_kw(db, uid, key, model, method, args, kw or {})

    return rpc


def plan_for(deal):
    """Return (group, vals, note) or (None, None, reason-to-skip)."""
    name = deal.get('name') or ''
    dtype = deal.get('discount_type') or None
    arrow = ARROW.search(name)
    nfor = NFOR.search(name)

    if dtype == 'percent' and arrow:
        orig, new = money(arrow.group(1)), money(arrow.group(2))
        if orig <= 0 or new < 0 or new > orig:
            return None, None, 'implausible arrow prices'
        pct = round((1 - new / orig) * 100, 2)
        stated = PCT.search(name)
        if stated and abs(float(stated.group(1)) - pct) > 1.0:
            # The name states a percentage that disagrees with its own prices.
            return None, None, 'name states %s%% but prices imply %s%%' % (stated.group(1), pct)
        # percent deals carry a whole percentage; the mapper divides by 100.
        return 'percent', {'discount_value': pct, 'original_price': orig}, '%s%% off' % pct

    if dtype == 'price' and arrow:
        orig, new = money(arrow.group(1)), money(arrow.group(2))
        if orig <= 0 or new < 0:
            return None, None, 'implausible arrow prices'
        # 'price' maps to price_to_amount: the value IS the resulting price.
        return 'price', {'discount_value': new, 'original_price': orig}, 'price -> $%s' % new

    if dtype == 'bundle' and nfor:
        # Names routinely carry EVERY tier: "1 for $15 2 for $25 3 for $55".
        # Capture them all — a single-tier read would publish the cheapest
        # entry as the whole deal. The qty-1 entry is the reference price,
        # not a bundle tier, so it becomes original_price instead.
        tiers, base = [], None
        for m in NFOR.finditer(name):
            qty, total = int(m.group(1)), money(m.group(2))
            if total <= 0:
                continue
            if qty == 1:
                base = total if base is None else min(base, total)
            elif qty >= 2:
                tiers.append((qty, total))
        tiers = sorted(set(tiers))
        if not tiers:
            return None, None, 'no multi-item tier in name'
        vals = {'bundle_tier_ids': [
            (0, 0, {'sequence': (i + 1) * 10, 'qty': q, 'price': p})
            for i, (q, p) in enumerate(tiers)]}
        if base:
            vals['original_price'] = base
        return 'bundle', vals, ' / '.join('%d for $%s' % (q, p) for q, p in tiers)

    if dtype == 'bogo':
        return None, None, 'BOGO: buy/get quantities are not recoverable from the name'
    if not dtype:
        return None, None, 'deal has no discount_type'
    return None, None, 'no price pattern in name'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true', help='write changes (default: dry run)')
    ap.add_argument('--rollback', metavar='FILE', help='restore prior values from a rollback file')
    ap.add_argument('--env', default='~/code/letsgomint-us/.env')
    ap.add_argument('--limit', type=int, default=0)
    args = ap.parse_args()

    rpc = connect(args.env)

    if args.rollback:
        data = json.load(open(args.rollback))
        for row in data['deals']:
            rpc('mint.ptl.deal', 'write', [[row['id']], row['before']])
        print('rolled back %d deals from %s' % (len(data['deals']), args.rollback))
        return

    zero = rpc('mint.discount', 'search_read',
               [[['is_published', '=', True], ['source', '=', 'ptl'],
                 '|', ['discount_amount', '=', 0], ['discount_amount', '=', False]]],
               {'fields': ['ptl_deal_id'], 'limit': args.limit or 2000})
    ids = [r['ptl_deal_id'][0] for r in zero if r.get('ptl_deal_id')]
    deals = rpc('mint.ptl.deal', 'read',
                [ids, ['id', 'name', 'discount_type', 'discount_value', 'original_price']])

    planned, skipped = [], []
    for d in deals:
        group, vals, note = plan_for(d)
        if vals:
            planned.append((d, group, vals, note))
        else:
            skipped.append((d, note))

    print('deals examined : %d' % len(deals))
    print('will change    : %d' % len(planned))
    print('skipped        : %d' % len(skipped))
    groups = {}
    for _d, g, _v, _n in planned:
        groups[g] = groups.get(g, 0) + 1
    for g, n in sorted(groups.items()):
        print('   %-8s %d' % (g, n))
    reasons = {}
    for _d, note in skipped:
        reasons[note] = reasons.get(note, 0) + 1
    print('   -- skip reasons --')
    for r, n in sorted(reasons.items(), key=lambda x: -x[1]):
        print('   %4d  %s' % (n, r))

    print('\nsample of planned writes:')
    for d, g, vals, note in planned[:8]:
        print('   [%s] %-46s %s' % (g, (d.get('name') or '')[:46], note))

    if not args.apply:
        print('\nDRY RUN — nothing written. Re-run with --apply to write.')
        return

    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    path = 'ptl_price_backfill_rollback_%s.json' % stamp
    rollback = {'created': stamp, 'deals': []}
    for d, _g, vals, _n in planned:
        before = {k: d.get(k) for k in TRACKED if k in vals}
        if 'bundle_tier_ids' in vals:
            before['_note'] = 'bundle tier created; remove it to undo'
        rollback['deals'].append({'id': d['id'], 'before': before})
    json.dump(rollback, open(path, 'w'), indent=1)
    print('\nrollback written to %s' % path)

    done = 0
    for d, _g, vals, _n in planned:
        rpc('mint.ptl.deal', 'write', [[d['id']], vals])
        done += 1
    print('wrote %d deals' % done)

    print('\nNEXT STEP — the discounts still hold the old values.')
    print('  _ensure_discount() rewrites mint.discount from the deal, so the new')
    print('  prices reach the storefront when the affected PTL days are published')
    print('  again through the normal flow. That also fires the inventory-service')
    print('  webhook, so it is deliberately left to a human rather than done here.')


if __name__ == '__main__':
    main()
