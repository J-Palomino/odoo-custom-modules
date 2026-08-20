"""Throwaway proof that the Odoo spreadsheet write path works end to end."""
import base64
import json

from odoo import Odoo, build_sheet, build_document

NAME = 'ZZ Canary — schedule sync probe'

grid = [
    ['', 'MONDAY', '', 'TUESDAY', ''],
    ['', '8/17/2026', '', '8/18/2026', ''],
    ['', 'Begin', 'End', 'Begin', 'End'],
    ['Emma', '1:30:00 PM', '9:30:00 PM', 'OFF', ''],
    ['Coop', '7:00:00 AM', '4:00:00 PM', '7:00:00 AM', '4:00:00 PM'],
]

o = Odoo()
doc = build_document([build_sheet('8/17-8/23', grid, merges=['B1:C1', 'D1:E1'], index=0)])
rid, created = o.upsert_spreadsheet(NAME, doc)
print(f'{"created" if created else "updated"} id={rid}')

# Read it back and confirm Odoo stored what we sent.
rec = o.execute('spreadsheet.spreadsheet', 'read', [rid],
                fields=['name', 'spreadsheet_raw', 'spreadsheet_binary_data', 'owner_id'])[0]
raw = rec['spreadsheet_raw']
if isinstance(raw, str):
    raw = json.loads(raw)

if raw and raw.get('sheets'):
    s = raw['sheets'][0]
    print('readback via spreadsheet_raw:')
    print('  owner        :', rec['owner_id'])
    print('  tab name     :', s['name'])
    print('  dims         :', s['colNumber'], 'cols x', s['rowNumber'], 'rows')
    print('  cell count   :', len(s['cells']))
    print('  merges       :', s['merges'])
    print('  A4 / B4      :', repr(s['cells'].get('A4')), '/', repr(s['cells'].get('B4')))
    print('  D4 (OFF)     :', repr(s['cells'].get('D4')))
else:
    print('spreadsheet_raw empty — decoding binary instead')
    d = json.loads(base64.b64decode(rec['spreadsheet_binary_data']))
    s = d['sheets'][0]
    print('  tab:', s['name'], '| cells:', len(s['cells']), '| merges:', s['merges'])

o.execute('spreadsheet.spreadsheet', 'unlink', [rid])
print('canary removed:', not o.execute(
    'spreadsheet.spreadsheet', 'search_count', [['name', '=', NAME]]))
