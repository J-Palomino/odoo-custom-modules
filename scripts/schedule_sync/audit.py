#!/usr/bin/env python3
"""Audit how much of each Google Sheet we actually imported.

Two separate questions, reported separately:

  COVERAGE — how many of the workbook's tabs we sync at all. By design this is
  a window around today, so historical weeks are deliberately excluded.

  FIDELITY — for the tabs we DO sync, whether every non-empty source cell made
  it into Odoo. Any shortfall here is a bug, not a design choice.

    python3 audit.py            # all stores
    python3 audit.py --store Tempe
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import STORES, odoo_name
import sheets as gs
from odoo import Odoo

CACHE = os.path.expanduser('~/.cache/mint-schedule-sync')


def source_tabs(st, token, cache_dir):
    """Return (all_tabs, selected_tabs) from the source, however we can read it."""
    path = os.path.join(cache_dir, f'{st["store"]}.xlsx')
    try:
        if not os.path.exists(path):
            gs.download_xlsx(st['sheet_id'], path, token=token)
        tabs = gs.read_workbook(path)
        return tabs, gs.select_weeks(tabs), 'xlsx'
    except gs.ExportTooLarge:
        sel = gs.read_via_api(st['sheet_id'], token)
        return None, sel, 'api'


def nonempty(grid):
    return sum(1 for row in grid for c in row if str(c).strip())


def dims(grid):
    return len(grid), max((len(r) for r in grid), default=0)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--store', action='append')
    p.add_argument('--cache-dir', default=CACHE)
    a = p.parse_args()

    targets = STORES
    if a.store:
        want = {s.lower() for s in a.store}
        targets = [s for s in STORES if s['store'].lower() in want]

    token, how = gs.get_token(probe_file_ids=[t['sheet_id'] for t in targets])
    print(f'google auth: {how}')
    o = Odoo()

    print(f'\n{"store":<12}{"tabs":>6}{"synced":>7}{"src cells":>11}'
          f'{"odoo cells":>12}{"delta":>8}  flags')
    print('-' * 74)

    problems = []
    for st in targets:
        all_tabs, sel, mode = source_tabs(st, token, a.cache_dir)
        src_cells = sum(nonempty(t['grid']) for t in sel)
        maxr = max((dims(t['grid'])[0] for t in sel), default=0)
        maxc = max((dims(t['grid'])[1] for t in sel), default=0)

        rec = o.execute('spreadsheet.spreadsheet', 'search_read',
                        [['name', '=', odoo_name(st['store'])]],
                        fields=['spreadsheet_raw'], limit=1)
        odoo_cells = 0
        if rec:
            raw = rec[0]['spreadsheet_raw']
            if isinstance(raw, str):
                raw = json.loads(raw)
            odoo_cells = sum(len(s['cells']) for s in raw.get('sheets', []))

        flags = []
        if mode == 'api':
            flags.append('api')
            if maxr >= 200:
                flags.append('ROW CAP HIT (A1:BZ200)')
            if maxc >= 78:
                flags.append('COL CAP HIT')
        n_all = len(all_tabs) if all_tabs is not None else '?'
        if all_tabs is not None:
            undated = sum(1 for t in all_tabs if not t['week_start'])
            if undated:
                flags.append(f'{undated} undated tab(s)')
        delta = odoo_cells - src_cells
        if delta != 0:
            flags.append(f'MISMATCH {delta:+d}')
            problems.append(st['store'])

        print(f'{st["store"]:<12}{str(n_all):>6}{len(sel):>7}{src_cells:>11}'
              f'{odoo_cells:>12}{delta:>8}  {", ".join(flags)}')

    print('-' * 74)
    if problems:
        print(f'FIDELITY MISMATCH in: {", ".join(problems)}')
    else:
        print('fidelity OK — every non-empty source cell in the synced tabs is '
              'present in Odoo')


if __name__ == '__main__':
    main()
