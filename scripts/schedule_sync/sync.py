#!/usr/bin/env python3
"""Mirror the Mint store schedule Google Sheets into Odoo spreadsheets.

Phase 0 of the schedule integration: this copies the grids in verbatim so the
schedules are visible inside Odoo. It deliberately does NOT interpret shifts or
touch hr.employee — Odoo has no shift model on this instance, and most of the
names on these sheets have no employee record yet.

Usage:
    python3 sync.py                     # all stores, week window around today
    python3 sync.py --store Mesa        # one store
    python3 sync.py --all-weeks         # every tab (large: Tempe has 176)
    python3 sync.py --dry-run           # parse and report, write nothing
    python3 sync.py --from-xlsx DIR     # use already-downloaded .xlsx files
"""

import argparse
import datetime as dt
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import STORES, odoo_name
import sheets as gs
from odoo import Odoo, build_sheet, build_document

CACHE = os.path.expanduser('~/.cache/mint-schedule-sync')


def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--store', action='append',
                   help='store name (repeatable); default all')
    p.add_argument('--all-weeks', action='store_true',
                   help='sync every tab instead of the window around today')
    p.add_argument('--back-days', type=int, default=7)
    p.add_argument('--forward-days', type=int, default=14)
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--from-xlsx', metavar='DIR',
                   help='read <store>.xlsx from DIR instead of downloading')
    p.add_argument('--cache-dir', default=CACHE)
    return p.parse_args()


def resolve_stores(names):
    if not names:
        return STORES
    want = {n.lower() for n in names}
    picked = [s for s in STORES if s['store'].lower() in want]
    missing = want - {s['store'].lower() for s in picked}
    if missing:
        raise SystemExit(f'unknown store(s): {", ".join(sorted(missing))}\n'
                         f'known: {", ".join(s["store"] for s in STORES)}')
    return picked


def main():
    args = parse_args()
    targets = resolve_stores(args.store)

    token = None
    if not args.from_xlsx:
        try:
            # probe against every target, so a credential that exists but
            # cannot see all the sheets is rejected rather than used
            token, how = gs.get_token(
                probe_file_ids=[t['sheet_id'] for t in targets])
            print(f'google auth: {how}')
        except gs.AuthError as e:
            raise SystemExit(f'\n{e}\n')

    odoo = None
    if not args.dry_run:
        odoo = Odoo()
        print(f'odoo: connected uid={odoo.uid}')

    print(f'{"store":<12} {"tabs":>5} {"weeks":>6} {"cells":>8}  result')
    print('-' * 62)

    failures = []
    for st in targets:
        name = st['store']
        try:
            via_api = False
            if args.from_xlsx:
                path = os.path.join(args.from_xlsx, f'{name}.xlsx')
                if not os.path.exists(path):
                    raise FileNotFoundError(path)
            else:
                path = os.path.join(args.cache_dir, f'{name}.xlsx')
                try:
                    gs.download_xlsx(st['sheet_id'], path, token=token)
                except gs.ExportTooLarge:
                    # Drive refuses to export workbooks past a size limit
                    # (Tempe, 176 weeks). Fall back to the Sheets API, which
                    # fetches only the weeks we want.
                    via_api = True

            if via_api:
                picked = gs.read_via_api(
                    st['sheet_id'], token, want_all=args.all_weeks,
                    back_days=args.back_days, forward_days=args.forward_days)
                tabs = picked
            else:
                tabs = gs.read_workbook(path)
                picked = tabs if args.all_weeks else gs.select_weeks(
                    tabs, back_days=args.back_days,
                    forward_days=args.forward_days)

            osheets = [
                build_sheet(t['title'], t['grid'], merges=t['merges'], index=i)
                for i, t in enumerate(picked)
            ]
            cells = sum(len(s['cells']) for s in osheets)

            if args.dry_run:
                weeks = ', '.join(
                    t['week_start'].isoformat() if t['week_start'] else t['title']
                    for t in picked)
                print(f'{name:<12} {len(tabs):>5} {len(picked):>6} {cells:>8}  '
                      f'dry-run [{weeks}]')
                continue

            doc = build_document(osheets)
            cid = odoo.company_id_by_name(st['company'])
            rid, created = odoo.upsert_spreadsheet(
                odoo_name(name, archive=args.all_weeks), doc, company_id=cid)
            verb = 'created' if created else 'updated'
            print(f'{name:<12} {len(tabs):>5} {len(picked):>6} {cells:>8}  '
                  f'{verb} id={rid}')

        except Exception as e:
            failures.append((name, e))
            print(f'{name:<12} {"-":>5} {"-":>6} {"-":>8}  FAILED: '
                  f'{type(e).__name__}: {str(e)[:70]}')

    if failures:
        print(f'\n{len(failures)} store(s) failed:')
        for n, e in failures:
            print(f'  {n}: {type(e).__name__}: {str(e)[:200]}')
        if os.environ.get('SCHEDULE_SYNC_TRACE'):
            traceback.print_exception(failures[0][1])
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
