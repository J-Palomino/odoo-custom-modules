# -*- coding: utf-8 -*-
"""Render parsed shifts into Odoo calendar.event records, idempotently.

Each shift gets a deterministic external ID in ir.model.data, so a re-run
updates in place instead of duplicating, and shifts that disappear from the
sheet can be found and removed.

Invitations are suppressed. Verified on a canary: creating an event with this
context queued zero mail.mail rows.
"""
import argparse
import collections
import datetime as dt
import os
import re
import sys
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from odoo import Odoo
from config import STORES
import shifts as SH

MODULE = 'mint_schedule_cal'
TAG = 'Store Schedule'
PHOENIX_OFFSET = dt.timedelta(hours=7)      # America/Phoenix, no DST

CTX = {
    'no_mail_to_attendees': True,
    'mail_create_nolog': True,
    'mail_create_nosubscribe': True,
    'mail_notify_force_send': False,
    'mail_auto_subscribe_no_notify': True,
    'tracking_disable': True,
}


def norm(s):
    s = unicodedata.normalize('NFKD', s or '').encode('ascii', 'ignore').decode()
    return re.sub(r'\s+', ' ', re.sub(r'[^A-Za-z ]', ' ', s)).strip().lower()


def build_index(recs):
    full, first = {}, collections.defaultdict(list)
    for r in recs:
        n = norm(r['name'])
        if n:
            full.setdefault(n, r)
            first[n.split()[0]].append(r)
    return full, first


def resolve(raw, full, first):
    n = norm(raw)
    if not n:
        return None
    if n in full:
        return full[n]
    parts = n.split()
    cands = list(first.get(parts[0], []))
    if len(parts) > 1 and cands:
        init = parts[1][:1]
        narrowed = [c for c in cands if len(norm(c['name']).split()) > 1
                    and norm(c['name']).split()[1][:1] == init]
        cands = narrowed or cands
    return cands[0] if len(cands) == 1 else None


def xmlid(store, emp_id, day, start):
    slug = re.sub(r'[^a-z0-9]+', '_', store.lower()).strip('_')
    return f'shift_{slug}_{emp_id}_{day:%Y%m%d}_{start:%H%M}'


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--store', action='append')
    p.add_argument('--apply', action='store_true')
    p.add_argument('--limit', type=int, default=0)
    a = p.parse_args()

    o = Odoo()
    X = o.execute
    targets = [s for s in STORES
               if not a.store or s['store'].lower() in {x.lower() for x in a.store}]

    emps = X('hr.employee', 'search_read', [['active', '=', True]],
             fields=['name', 'work_contact_id', 'company_id'])
    full, first = build_index(emps)

    tag = X('calendar.event.type', 'search_read', [['name', '=', TAG]],
            fields=['id'], limit=1)
    tag_id = tag[0]['id'] if tag else None
    if not tag_id and a.apply:
        tag_id = X('calendar.event.type', 'create', {'name': TAG})
        print(f"created calendar.event.type '{TAG}' id={tag_id}")

    created = updated = skipped_noemp = 0
    unresolved = collections.Counter()
    rows = []

    for st in targets:
        for sheet in SH.load(st['store']):
            parsed, _ = SH.parse_tab(sheet)
            for s in parsed:
                emp = resolve(s['name'], full, first)
                if not emp or not emp.get('work_contact_id'):
                    unresolved[f"{st['store']}: {s['name']}"] += 1
                    skipped_noemp += 1
                    continue
                start = dt.datetime.combine(s['date'], s['start']) + PHOENIX_OFFSET
                stop = start + dt.timedelta(hours=s['hours'])
                rows.append({
                    'key': xmlid(st['store'], emp['id'], s['date'], s['start']),
                    'store': st['store'], 'emp': emp, 'shift': s,
                    'start': start, 'stop': stop,
                })

    if a.limit:
        rows = rows[:a.limit]

    print(f"shifts renderable : {len(rows)}")
    print(f"skipped, no employee record: {skipped_noemp} "
          f"({len(unresolved)} distinct names)")
    if not a.apply:
        print("\nsample:")
        for r in rows[:5]:
            print(f"  {r['store']:<11} {r['emp']['name'][:22]:<22} "
                  f"{r['shift']['date']} "
                  f"{r['shift']['start'].strftime('%H:%M')}-"
                  f"{r['shift']['end'].strftime('%H:%M')}  {r['key']}")
        print("\nDRY RUN — nothing written. Re-run with --apply")
        return

    # existing external ids for this module
    imd = X('ir.model.data', 'search_read', [['module', '=', MODULE]],
            fields=['name', 'res_id'])
    existing = {r['name']: r for r in imd}
    print(f"existing rendered shifts: {len(existing)}")

    # Batch the writes. One call per record rate-limits the server (618 shifts
    # is ~1,200 round trips and earns a 429); Odoo's create() takes a list, so
    # this collapses to a handful of calls.
    to_create, to_update = [], []
    for r in rows:
        vals = {
            'name': f"{r['store']} — {r['emp']['name']}",
            'start': r['start'].strftime('%Y-%m-%d %H:%M:%S'),
            'stop': r['stop'].strftime('%Y-%m-%d %H:%M:%S'),
            'allday': False,
            'partner_ids': [(6, 0, [r['emp']['work_contact_id'][0]])],
            'alarm_ids': [(5, 0, 0)],
            'show_as': 'free',
            'description': (f"Store schedule · {r['store']} · "
                            f"{r['shift']['start'].strftime('%-I:%M %p')}–"
                            f"{r['shift']['end'].strftime('%-I:%M %p')} "
                            f"({r['shift']['hours']}h clock time)"),
        }
        if tag_id:
            vals['categ_ids'] = [(6, 0, [tag_id])]

        prior = existing.get(r['key'])
        if prior:
            to_update.append((prior['res_id'], vals))
        else:
            to_create.append((r['key'], vals))

    CHUNK = 50
    for i in range(0, len(to_create), CHUNK):
        part = to_create[i:i + CHUNK]
        ids = X('calendar.event', 'create', [v for _, v in part], context=CTX)
        if isinstance(ids, int):
            ids = [ids]
        X('ir.model.data', 'create',
          [{'module': MODULE, 'name': k, 'model': 'calendar.event', 'res_id': eid}
           for (k, _), eid in zip(part, ids)])
        created += len(part)
        print(f"  created {created}/{len(to_create)}")

    for i in range(0, len(to_update), CHUNK):
        part = to_update[i:i + CHUNK]
        # same-shaped vals differ per record, so write individually within the
        # chunk but keep the loop bounded
        for rid, vals in part:
            X('calendar.event', 'write', [rid], vals, context=CTX)
        updated += len(part)

    print(f"\ncreated {created}, updated {updated}")


if __name__ == '__main__':
    main()
