#!/usr/bin/env python3
"""Extract the people named on the schedules we imported, and resolve each one
against Odoo's roster.

Reads the synced `spreadsheet.spreadsheet` records back out of Odoo — so this
reports on exactly what was imported, not on a sample of the source sheets —
then matches every name against hr.employee and against internal res.users.

Writes a CSV of every name with its status, which is the artefact HR needs.

    python3 roster_report.py [--csv PATH]
"""

import argparse
import collections
import csv
import json
import os
import re
import sys
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import STORES, odoo_name
from odoo import Odoo, col_letter

# Rows that occupy the name column but are not people. Matched case-insensitively
# on the whole cell after normalisation.
NOT_A_NAME = {
    'admin', 'administration', 'am shift', 'pm shift', 'mid shift', 'flex', 'flex mid',
    'boh', 'boh clerk', 'foh', 'leads', 'lead', 'leadership', 'leadership team',
    'inventory', 'management', 'managemenyt', 'psr', 'psrs', 'psr am', 'psr pm',
    'shift captains', 'captn', 'captain', 'drive thru support', 'dt support',
    'totals', 'total', 'needed', 'wages', 'sales', 'labor', 'labor %', 'hours',
    'begin', 'end', 'start', 'subject to change', 'open', 'new hire', 'off',
    'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday',
    'early birds', 'hybrid heroes', 'nocturnal nugs', 'admin sativa', 'admin indica',
    'host inventory admin', 'sales associate', 'openers', 'mids', 'closers',
    'no set schedules', 'audit week subject to change', 'week', 'name', 'employee',
    'pto', 'req off', 'requested off', 'on call', 'tbd', 'mia', 'loa', 'bell',
    'overstock', 'training', 'holiday', 'onboarding', 'audit', 'reference sheet',
    'el mirage reference sheet', 'nocturnal', 'store', 'notes', 'note', 'time',
}

TIMEISH = re.compile(r'^\d{1,2}:\d{2}(:\d{2})?\s*(am|pm)?$', re.I)
NUMERIC = re.compile(r'^[\$\d.,%\-\s]+$')
DATEISH = re.compile(r'\d{1,2}/\d{1,2}/\d{2,4}')


def norm(s):
    s = unicodedata.normalize('NFKD', s or '').encode('ascii', 'ignore').decode()
    return re.sub(r'\s+', ' ', re.sub(r'[^A-Za-z ]', ' ', s)).strip().lower()


def looks_like_name(v):
    if not v:
        return False
    t = v.strip()
    if len(t) < 2 or len(t) > 40:
        return False
    if TIMEISH.match(t) or NUMERIC.match(t) or DATEISH.search(t):
        return False
    n = norm(t)
    if not n or n in NOT_A_NAME:
        return False
    # must contain at least two letters and not be mostly punctuation
    return len(n.replace(' ', '')) >= 2


def names_from_sheet(sheet):
    """The name column is whichever of the first two columns carries the most
    name-like values — layouts differ (some put a weekly-hours total in col 0)."""
    cells = sheet['cells']
    rows = sheet['rowNumber']
    best, best_hits = 0, -1
    for c in (0, 1, 2):
        hits = sum(1 for r in range(rows)
                   if looks_like_name(cells.get(f'{col_letter(c)}{r + 1}', '')))
        if hits > best_hits:
            best, best_hits = c, hits
    out = []
    for r in range(rows):
        v = cells.get(f'{col_letter(best)}{r + 1}', '')
        if looks_like_name(v):
            out.append(v.strip())
    return out


def build_index(records):
    by_full, by_first = {}, collections.defaultdict(list)
    for rec in records:
        n = norm(rec['name'])
        if not n:
            continue
        by_full.setdefault(n, rec)
        by_first[n.split()[0]].append(rec)
    return by_full, by_first


def resolve(raw, by_full, by_first):
    n = norm(raw)
    if not n:
        return 'SKIP', ''
    if n in by_full:
        return 'OK', by_full[n]['name']
    parts = n.split()
    cands = list(by_first.get(parts[0], []))
    if len(parts) > 1 and cands:
        init = parts[1][:1]
        narrowed = [c for c in cands
                    if len(norm(c['name']).split()) > 1
                    and norm(c['name']).split()[1][:1] == init]
        cands = narrowed or cands
    if len(cands) == 1:
        return 'OK', cands[0]['name']
    if not cands:
        return 'MISSING', ''
    return 'AMBIGUOUS', '; '.join(c['name'] for c in cands[:5])


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--csv', default='roster_gap.csv')
    a = p.parse_args()

    o = Odoo()
    emps = o.execute('hr.employee', 'search_read', [['active', '=', True]],
                     fields=['name'])
    users = o.execute('res.users', 'search_read',
                      [['active', '=', True], ['share', '=', False]],
                      fields=['name', 'login'])
    e_full, e_first = build_index(emps)
    u_full, u_first = build_index(users)

    rows = []
    per_store = []
    for st in STORES:
        rec = o.execute('spreadsheet.spreadsheet', 'search_read',
                        [['name', '=', odoo_name(st['store'])]],
                        fields=['spreadsheet_raw'], limit=1)
        if not rec:
            continue
        raw = rec[0]['spreadsheet_raw']
        if isinstance(raw, str):
            raw = json.loads(raw)
        seen = set()
        for sheet in raw.get('sheets', []):
            for nm in names_from_sheet(sheet):
                key = norm(nm)
                if key in seen:
                    continue
                seen.add(key)
                es, ed = resolve(nm, e_full, e_first)
                us, ud = resolve(nm, u_full, u_first)
                rows.append({'store': st['store'], 'name': nm,
                             'employee_status': es, 'employee_match': ed,
                             'user_status': us, 'user_match': ud})
        c = collections.Counter(r['employee_status'] for r in rows
                                if r['store'] == st['store'])
        per_store.append((st['store'], len(seen), c))

    with open(a.csv, 'w', newline='') as fh:
        w = csv.DictWriter(fh, fieldnames=['store', 'name', 'employee_status',
                                           'employee_match', 'user_status',
                                           'user_match'])
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: (r['employee_status'] != 'MISSING',
                                                r['store'], r['name'])))

    print(f'{"store":<12}{"names":>7}{"matched":>9}{"ambig":>7}{"missing":>9}')
    print('-' * 44)
    for store, n, c in per_store:
        print(f'{store:<12}{n:>7}{c["OK"]:>9}{c["AMBIGUOUS"]:>7}{c["MISSING"]:>9}')
    print('-' * 44)
    tot = collections.Counter(r['employee_status'] for r in rows)
    utot = collections.Counter(r['user_status'] for r in rows)
    uniq = len({norm(r['name']) for r in rows})
    print(f'{"TOTAL":<12}{len(rows):>7}{tot["OK"]:>9}{tot["AMBIGUOUS"]:>7}'
          f'{tot["MISSING"]:>9}')
    print(f'\ndistinct people (across stores): {uniq}')
    print(f'vs hr.employee   : {tot["OK"]} matched, {tot["AMBIGUOUS"]} ambiguous, '
          f'{tot["MISSING"]} missing  ({100*tot["OK"]//max(len(rows),1)}%)')
    print(f'vs internal users: {utot["OK"]} matched, {utot["AMBIGUOUS"]} ambiguous, '
          f'{utot["MISSING"]} missing  ({100*utot["OK"]//max(len(rows),1)}%)')
    print(f'\nwrote {a.csv} ({len(rows)} rows)')


if __name__ == '__main__':
    main()
