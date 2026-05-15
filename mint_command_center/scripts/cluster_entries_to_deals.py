#!/usr/bin/env python3
"""
cluster_entries_to_deals.py — derive mint.ptl.deal records from clusters of
mint.brand.calendar.entry rows so the National Promo Log has start/end dates
and can be plotted onto PTL days.

Cluster key: (brand_id, market_id, sku_or_category, promo_text, discount_type,
             round(discount_value, 2), round(original_price or 0, 2))

For each cluster:
  • find_or_create mint.ptl.deal with:
      name           = "<brand> · <sku_or_category> · <promo_text-truncated>"
      brand_id, market_id, discount_type, discount_value, original_price
      sales_details  = promo_text (full text)
      date_start     = min(entry.date)
      date_end       = max(entry.date)
      state          = 'approved'
      campaign_id    = entries' campaign_id IFF unanimous, else null
  • write deal_id back onto every entry in the cluster.

Idempotent — re-runs detect existing deals via the cluster key (brand+market+
sku+promo_text+type+value) and reuse them, expanding date_start/date_end to
include any new entries.

Auth via env: ODOO_URL, ODOO_DB, ODOO_UID (default 2), ODOO_KEY.
Usage:
  python3 cluster_entries_to_deals.py --dry-run
  python3 cluster_entries_to_deals.py --market AZ
  python3 cluster_entries_to_deals.py
"""
import argparse
import json
import logging
import os
import sys
import urllib.request
from collections import defaultdict
from datetime import date

_log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')


class Odoo:
    def __init__(self, url, db, uid, key, dry_run=False):
        self.url = url.rstrip('/')
        if not self.url.endswith('/jsonrpc'):
            self.url += '/jsonrpc'
        self.db = db
        self.uid = uid
        self.key = key
        self.dry_run = dry_run
        self.headers = {
            'Content-Type': 'application/json',
            'User-Agent': 'mint-cluster/1.0',
            'Accept': 'application/json',
        }

    def call(self, model, method, args, kwargs=None):
        if self.dry_run and method in ('create', 'write', 'unlink'):
            _log.info('[DRY-RUN] %s.%s(args=%s, kw=%s)', model, method,
                      str(args)[:240], str(kwargs)[:120])
            return {'result': True if method != 'create' else 0}
        body = json.dumps({
            'jsonrpc': '2.0', 'method': 'call',
            'params': {
                'service': 'object', 'method': 'execute_kw',
                'args': [self.db, self.uid, self.key, model, method, args, kwargs or {}],
            },
        }).encode()
        with urllib.request.urlopen(
            urllib.request.Request(self.url, data=body, headers=self.headers),
            timeout=300,
        ) as resp:
            data = json.loads(resp.read())
        if 'error' in data:
            raise RuntimeError(data['error'].get('data', {}).get('message', data['error']))
        return data

    def search_read(self, model, domain, fields, **kw):
        return self.call(model, 'search_read', [domain], {'fields': fields, **kw}).get('result', [])

    def search_count(self, model, domain):
        return self.call(model, 'search_count', [domain]).get('result', 0)

    def create(self, model, vals):
        return self.call(model, 'create', [vals]).get('result')

    def write(self, model, ids, vals):
        return self.call(model, 'write', [ids, vals]).get('result')


# SKU column headers that are template metadata, not real product categories.
# These come from XLSX columns that sat past the Date column and got swept up
# as SKU columns by backfill_2026_national_promo_log.py. They produce nonsense
# deals like "Aeriz · Day of week · Thursday".
JUNK_SKUS = frozenset(s.lower() for s in (
    'day of week', 'holidays', 'weeks', 'date', 'notes',
    'ready to plot', 'plotted', 'old day specific d', 'new day specific d',
    'cost', 'msrp', 'sku', 'edlp',
))


def is_junk_entry(entry):
    sku = (entry.get('sku_or_category') or '').strip()
    if not sku:
        return True
    if sku.lower() in JUNK_SKUS:
        return True
    promo = (entry.get('promo_text') or '').strip()
    if not promo:
        return True
    # Day-name cells under "Day of week" column
    if promo.lower() in {'monday', 'tuesday', 'wednesday', 'thursday',
                         'friday', 'saturday', 'sunday'}:
        return True
    return False


def cluster_key(entry):
    """The deal-identifying tuple for a calendar entry."""
    brand = entry['brand_id'][0] if entry.get('brand_id') else None
    market = entry['market_id'][0] if entry.get('market_id') else None
    return (
        brand,
        market,
        (entry.get('sku_or_category') or '').strip(),
        (entry.get('promo_text') or '').strip(),
        entry.get('discount_type') or '',
        round(entry.get('discount_value') or 0.0, 2),
        round(entry.get('original_price') or 0.0, 2),
    )


def derive_deal_name(brand_name: str, sku: str, promo: str) -> str:
    parts = [p for p in [brand_name, sku, promo] if p]
    name = ' · '.join(parts)
    return name[:200]


def fetch_all_entries(odoo, market_filter=None):
    """Stream every mint.brand.calendar.entry row in pages."""
    domain = []
    if market_filter:
        # Need to resolve market code → id before this is called
        domain.append(['market_id.code', '=', market_filter])
    out = []
    offset = 0
    page_size = 1000
    while True:
        page = odoo.search_read(
            'mint.brand.calendar.entry', domain,
            ['id', 'brand_id', 'market_id', 'date', 'sku_or_category',
             'promo_text', 'discount_type', 'discount_value', 'original_price',
             'campaign_id', 'deal_id'],
            limit=page_size, offset=offset, order='id',
        )
        if not page:
            break
        out.extend(page)
        _log.info('  fetched %d entries (running total %d)', len(page), len(out))
        offset += page_size
    return out


def find_existing_deal(odoo, key, brand_name=None):
    """Look up an existing mint.ptl.deal that matches this cluster key.

    Cluster-equivalent: same brand_id + market_id + name pattern. We use the
    name we'd assign as the canonical match (it encodes brand+sku+promo).
    """
    brand_id, market_id, sku, promo, dtype, dvalue, oprice = key
    # Match on name (which is unique enough for our derived deals) and brand
    expected_name = derive_deal_name(brand_name or '', sku, promo)
    if not expected_name:
        return None
    existing = odoo.search_read(
        'mint.ptl.deal',
        [['name', '=', expected_name], ['brand_id', '=', brand_id],
         ['market_id', '=', market_id]],
        ['id'], limit=1,
    )
    return existing[0]['id'] if existing else None


def upsert_deal(odoo, key, entries, brand_name_cache):
    brand_id, market_id, sku, promo, dtype, dvalue, oprice = key
    if not brand_id or not market_id:
        return None, 'skip_no_brand_or_market'

    # Date range
    dates = [e['date'] for e in entries if e.get('date')]
    if not dates:
        return None, 'skip_no_dates'
    date_start = min(dates)
    date_end = max(dates)

    # Unanimous campaign?
    camp_ids = {e['campaign_id'][0] for e in entries if e.get('campaign_id')}
    campaign_id = next(iter(camp_ids)) if len(camp_ids) == 1 else False

    brand_name = brand_name_cache.get(brand_id, '')
    deal_name = derive_deal_name(brand_name, sku, promo)
    vals = {
        'name': deal_name,
        'brand_id': brand_id,
        'market_id': market_id,
        'product_category': sku or False,
        'discount_type': dtype or False,
        'discount_value': dvalue if dvalue else False,
        'original_price': oprice if oprice else False,
        'sales_details': promo or False,
        'date_start': date_start,
        'date_end': date_end,
        'state': 'approved',
    }
    if campaign_id:
        vals['campaign_id'] = campaign_id

    existing_id = find_existing_deal(odoo, key, brand_name=brand_name)
    if existing_id:
        # Expand date range only (don't clobber other manually-set fields)
        cur = odoo.search_read(
            'mint.ptl.deal', [['id', '=', existing_id]],
            ['date_start', 'date_end'], limit=1,
        )[0]
        new_start = min(date_start, cur['date_start']) if cur['date_start'] else date_start
        new_end = max(date_end, cur['date_end']) if cur['date_end'] else date_end
        update = {}
        if cur['date_start'] != new_start:
            update['date_start'] = new_start
        if cur['date_end'] != new_end:
            update['date_end'] = new_end
        if update:
            odoo.write('mint.ptl.deal', [existing_id], update)
        return existing_id, 'reused'

    new_id = odoo.create('mint.ptl.deal', vals)
    return new_id, 'created'


def link_entries_to_deal(odoo, deal_id, entry_ids, dry_run=False):
    """Set deal_id on each entry that's not already linked."""
    if not entry_ids:
        return 0
    # Filter out entries already linked
    if not dry_run:
        already = odoo.search_read(
            'mint.brand.calendar.entry',
            [['id', 'in', entry_ids], ['deal_id', '=', deal_id]],
            ['id'], limit=len(entry_ids),
        )
        already_set = {r['id'] for r in already}
        entry_ids = [eid for eid in entry_ids if eid not in already_set]
        if not entry_ids:
            return 0
    # Write in chunks
    written = 0
    for i in range(0, len(entry_ids), 200):
        chunk = entry_ids[i:i + 200]
        odoo.write('mint.brand.calendar.entry', chunk, {'deal_id': deal_id})
        written += len(chunk)
    return written


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--market', help='Limit to one market code (AZ / IL / MO / NV)')
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--limit-clusters', type=int, default=0,
                   help='Stop after N clusters (debugging)')
    p.add_argument('--url', default=os.environ.get('ODOO_URL', 'https://letsgomint.us/jsonrpc'))
    p.add_argument('--db', default=os.environ.get('ODOO_DB', 'odoo'))
    p.add_argument('--uid', type=int, default=int(os.environ.get('ODOO_UID', '2')))
    p.add_argument('--key', default=os.environ.get('ODOO_KEY', ''))
    args = p.parse_args()
    if not args.key:
        sys.exit('ODOO_KEY env var or --key required')

    odoo = Odoo(args.url, args.db, args.uid, args.key, dry_run=args.dry_run)

    _log.info('Loading mint.brand.calendar.entry...')
    entries = fetch_all_entries(odoo, market_filter=args.market)
    _log.info('  %d entries', len(entries))

    # Brand name cache (for deal naming)
    brand_ids = sorted({e['brand_id'][0] for e in entries if e.get('brand_id')})
    _log.info('  %d distinct brands; loading names...', len(brand_ids))
    brands = odoo.search_read(
        'mint.brand', [['id', 'in', brand_ids]], ['id', 'name'],
        limit=len(brand_ids),
    )
    brand_name_cache = {b['id']: b['name'] for b in brands}

    # Filter junk + cluster
    junk_count = 0
    clusters = defaultdict(list)
    for e in entries:
        if is_junk_entry(e):
            junk_count += 1
            continue
        clusters[cluster_key(e)].append(e)
    _log.info('  filtered out %d junk entries (template-header SKUs)', junk_count)
    _log.info('  %d distinct clusters', len(clusters))

    if args.limit_clusters:
        items = list(clusters.items())[:args.limit_clusters]
        clusters = dict(items)
        _log.info('  capped at %d clusters for debugging', len(clusters))

    counts = {'created': 0, 'reused': 0, 'skip_no_brand_or_market': 0,
              'skip_no_dates': 0, 'entries_linked': 0, 'entries_already_linked': 0}

    for i, (key, cluster_entries) in enumerate(clusters.items(), 1):
        if i % 200 == 0:
            _log.info('  progress: %d/%d clusters processed', i, len(clusters))
        deal_id, status = upsert_deal(odoo, key, cluster_entries, brand_name_cache)
        counts[status] = counts.get(status, 0) + 1
        if not deal_id:
            continue
        entry_ids = [e['id'] for e in cluster_entries]
        already_linked = sum(1 for e in cluster_entries
                             if e.get('deal_id') and e['deal_id'][0] == deal_id)
        counts['entries_already_linked'] += already_linked
        if not args.dry_run:
            written = link_entries_to_deal(odoo, deal_id, entry_ids)
            counts['entries_linked'] += written

    _log.info('done.')
    for k, v in sorted(counts.items()):
        _log.info('  %s: %d', k, v)


if __name__ == '__main__':
    main()
