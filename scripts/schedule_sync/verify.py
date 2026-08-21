#!/usr/bin/env python3
"""Read a synced schedule back out of Odoo and show what actually landed."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from odoo import Odoo, col_letter

name = sys.argv[1] if len(sys.argv) > 1 else 'Schedule — Mesa'
o = Odoo()
rec = o.execute('spreadsheet.spreadsheet', 'search_read', [['name', '=', name]],
                fields=['name', 'spreadsheet_raw', 'company_id', 'owner_id',
                        'create_date', 'write_date'], limit=1)
if not rec:
    raise SystemExit(f'not found: {name}')
rec = rec[0]
raw = rec['spreadsheet_raw']
if isinstance(raw, str):
    raw = json.loads(raw)

print(f"name    : {rec['name']}")
print(f"company : {rec['company_id']}")
print(f"owner   : {rec['owner_id']}")
print(f"written : {rec['write_date']}")
print(f"tabs    : {[s['name'] for s in raw['sheets']]}")

s = raw['sheets'][0]
cells = s['cells']
print(f"tab '{s['name']}': {s['colNumber']} cols x {s['rowNumber']} rows, "
      f"{len(cells)} cells\n")

# render the first 12 rows, first 8 columns, as stored
w = 13
for r in range(min(12, s['rowNumber'])):
    out = []
    for c in range(min(8, s['colNumber'])):
        v = cells.get(f'{col_letter(c)}{r + 1}', '')
        out.append((v[:w - 1] if len(v) > w - 1 else v).ljust(w))
    line = ''.join(out).rstrip()
    if line:
        print(f'{r + 1:>3} | {line}')
