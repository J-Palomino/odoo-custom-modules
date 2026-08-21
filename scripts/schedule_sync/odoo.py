"""Write mirrored schedule grids into Odoo as spreadsheet.spreadsheet records.

Document shape and the write field were both taken from a live record on prod
(`GL Daily Sales - 2026-08-19`) rather than from docs: cells are a flat
``{"A1": "value"}`` map of plain strings, and the payload goes in
``spreadsheet_binary_data`` as base64 JSON — not ``spreadsheet_raw``, which the
existing GL importer pointedly does not use.
"""

import base64
import json
import xmlrpc.client

from config import ODOO_URL, ENV_PATH

# o-spreadsheet version reported by the live records we are matching.
OSPREADSHEET_VERSION = '18.5.10'

LOCALE = {
    'name': 'English (US)',
    'code': 'en_US',
    'thousandsSeparator': ',',
    'decimalSeparator': '.',
    'dateFormat': 'm/d/yyyy',
    'timeFormat': 'hh:mm:ss a',
    'formulaArgSeparator': ',',
}


def col_letter(idx):
    """0 -> A, 25 -> Z, 26 -> AA."""
    s = ''
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        s = chr(65 + rem) + s
    return s


def a1(row, col):
    return f'{col_letter(col)}{row + 1}'


def _sheet_id(title, index):
    """o-spreadsheet needs a stable, unique sheet id per tab."""
    safe = ''.join(c if c.isalnum() else '_' for c in title).strip('_').lower()
    return f'wk_{index:03d}_{safe}'[:60] or f'wk_{index:03d}'


def build_sheet(title, grid, merges=None, index=0, name_col_width=150,
                cell_styles=None, style_table=None):
    """Turn one week's 2-D grid into an o-spreadsheet sheet dict.

    `cell_styles` maps (row, col) -> a style dict using the keys o-spreadsheet
    actually stores (verified against live records): bold, fillColor,
    textColor, fontSize, align, wrapping, verticalAlign. Styles are shared
    through `style_table`, a dict used to deduplicate across sheets — the same
    handful of colours repeats thousands of times.
    """
    cells = {}
    max_cols = 0
    for r, row in enumerate(grid):
        max_cols = max(max_cols, len(row))
        for c, val in enumerate(row):
            if val is None:
                continue
            text = str(val).strip()
            if text:
                cells[a1(r, c)] = text

    sheet_styles = {}
    if cell_styles and style_table is not None:
        for (r, c), props in cell_styles.items():
            if not props:
                continue
            key = json.dumps(props, sort_keys=True)
            sid = style_table.get(key)
            if sid is None:
                sid = len(style_table) + 1
                style_table[key] = sid
            sheet_styles[a1(r, c)] = sid

    return {
        'id': _sheet_id(title, index),
        'name': title[:100],
        'cells': cells,
        'colNumber': max(max_cols, 1),
        'rowNumber': max(len(grid), 1),
        # Widen the first column — every layout puts the employee name there.
        'cols': {'0': {'size': name_col_width}},
        'rows': {},
        'merges': list(merges or []),
        'styles': sheet_styles,
        'formats': {},
        'borders': {},
        'conditionalFormats': [],
        'dataValidationRules': [],
        'figures': [],
        'tables': [],
        'areGridLinesVisible': True,
        'isVisible': True,
        'headerGroups': {'ROW': [], 'COL': []},
    }


def build_document(sheets, style_table=None):
    """Wrap sheet dicts in the top-level o-spreadsheet document.

    `style_table` is the {props_json: id} map filled in by build_sheet; it is
    inverted here into the document's {id: props} styles map.
    """
    styles = {}
    if style_table:
        styles = {str(sid): json.loads(key) for key, sid in style_table.items()}
    return {
        'version': OSPREADSHEET_VERSION,
        'revisionId': 'START_REVISION',
        'sheets': sheets,
        'styles': styles,
        'formats': {},
        'borders': {},
        'uniqueFigureIds': True,
        'settings': {'locale': LOCALE},
        'pivots': {},
        'pivotNextId': 1,
        'customTableStyles': {},
        'globalFilters': [],
        'lists': {},
        'listNextId': 1,
        'chartOdooMenusReferences': {},
    }


class Odoo:
    def __init__(self, url=ODOO_URL, env_path=ENV_PATH):
        env = {}
        for line in open(env_path):
            line = line.strip()
            if line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            env[k] = v.strip().strip('"').strip("'")
        self.url = url
        self.db = env['ODOO_DB']
        self.key = env['ODOO_API_KEY']
        self.uid = xmlrpc.client.ServerProxy(
            f'{url}/xmlrpc/2/common').authenticate(
                self.db, env['ODOO_USERNAME'], self.key, {})
        if not self.uid:
            raise RuntimeError('Odoo authentication failed')
        self._models = xmlrpc.client.ServerProxy(f'{url}/xmlrpc/2/object')

    def execute(self, model, method, *args, **kw):
        return self._models.execute_kw(
            self.db, self.uid, self.key, model, method, list(args), kw)

    def upsert_spreadsheet(self, name, document, company_id=None):
        """Create or update by name. Returns (id, created)."""
        payload = base64.b64encode(
            json.dumps(document).encode('utf-8')).decode('ascii')

        existing = self.execute(
            'spreadsheet.spreadsheet', 'search_read',
            [['name', '=', name]], fields=['id'], limit=1)

        vals = {'spreadsheet_binary_data': payload}
        if company_id:
            vals['company_id'] = company_id

        if existing:
            rid = existing[0]['id']
            self.execute('spreadsheet.spreadsheet', 'write', [rid], vals)
            return rid, False

        vals['name'] = name
        rid = self.execute('spreadsheet.spreadsheet', 'create', vals)
        return rid, True

    def company_id_by_name(self, name):
        if not name:
            return None
        rec = self.execute('res.company', 'search_read',
                           [['name', '=', name]], fields=['id'], limit=1)
        return rec[0]['id'] if rec else None
