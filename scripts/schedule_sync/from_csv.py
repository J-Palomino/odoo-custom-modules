#!/usr/bin/env python3
"""Sync a single week grid from a base64-encoded CSV export into Odoo.

Bridge path for when Drive API access isn't wired up yet: a Google Sheets CSV
export (first tab only) can be fetched by other means and pushed through the
same builder the full sync uses.

    python3 from_csv.py --store Mesa --b64 mesa_tab1.b64 --tab "7/27-8/2"
"""

import argparse
import base64
import csv
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import odoo_name, STORES
from odoo import Odoo, build_sheet, build_document
from sheets import _dates_in


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--store', required=True)
    p.add_argument('--b64', required=True, help='file holding base64 CSV')
    p.add_argument('--tab', help='tab name; default derived from dates')
    p.add_argument('--dry-run', action='store_true')
    a = p.parse_args()

    st = next((s for s in STORES if s['store'].lower() == a.store.lower()), None)
    if not st:
        raise SystemExit(f'unknown store {a.store}')

    text = base64.b64decode(open(a.b64).read().strip()).decode('utf-8-sig')
    grid = [row for row in csv.reader(io.StringIO(text))]
    while grid and not any(c.strip() for c in grid[-1]):
        grid.pop()

    dates = _dates_in(grid)
    # 1/4/1900 and similar are spreadsheet epoch artefacts, not real schedule days
    real = [d for d in dates if d.year > 1900]
    tab = a.tab or (f'{real[0].month}/{real[0].day}-{real[-1].month}/{real[-1].day}'
                    if real else 'week')

    sheet = build_sheet(tab, grid, index=0)
    print(f'store       : {st["store"]}')
    print(f'tab         : {tab}')
    print(f'grid        : {len(grid)} rows x {max(len(r) for r in grid)} cols')
    print(f'cells       : {len(sheet["cells"])}')
    print(f'dates found : {[d.isoformat() for d in real]}')

    if a.dry_run:
        print('dry-run — nothing written')
        return

    o = Odoo()
    cid = o.company_id_by_name(st['company'])
    rid, created = o.upsert_spreadsheet(odoo_name(st['store']),
                                        build_document([sheet]), company_id=cid)
    print(f'odoo        : {"created" if created else "updated"} '
          f'spreadsheet.spreadsheet id={rid} company_id={cid}')


if __name__ == '__main__':
    main()
