#!/usr/bin/env python3
"""Read every synced schedule back out of Odoo and summarise what landed."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import STORES, odoo_name
from odoo import Odoo

o = Odoo()
print(f'{"store":<12} {"id":>5} {"company":<16} {"tabs":>5} {"cells":>7}  weeks')
print('-' * 78)
total = 0
for st in STORES:
    name = odoo_name(st['store'])
    rec = o.execute('spreadsheet.spreadsheet', 'search_read',
                    [['name', '=', name]],
                    fields=['spreadsheet_raw', 'company_id'], limit=1)
    if not rec:
        print(f'{st["store"]:<12} {"-":>5}  MISSING')
        continue
    rec = rec[0]
    raw = rec['spreadsheet_raw']
    if isinstance(raw, str):
        raw = json.loads(raw)
    sheets = raw.get('sheets', [])
    cells = sum(len(s['cells']) for s in sheets)
    total += cells
    co = rec['company_id'][1] if rec['company_id'] else '-'
    print(f'{st["store"]:<12} {rec["id"]:>5} {co:<16} {len(sheets):>5} '
          f'{cells:>7}  {", ".join(s["name"] for s in sheets)}')
print('-' * 78)
print(f'{"total":<12} {"":>5} {"":<16} {"":>5} {total:>7} cells')
