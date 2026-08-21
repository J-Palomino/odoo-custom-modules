# -*- coding: utf-8 -*-
"""Pull the store schedule Google Sheets into spreadsheet.spreadsheet records.

Everything is read through the Sheets API rather than a Drive XLSX export, for
two reasons: the export endpoint refuses workbooks past a size limit (Tempe
carries 225 tabs and trips it), and the API path needs nothing beyond
`requests`, which Odoo already ships. No openpyxl, no google-auth.

The document shape and the write field were taken from live records rather
than documentation: cells are a flat {"A1": "value"} map of plain strings, the
payload goes in `spreadsheet_binary_data` as base64 JSON, and styles are a
{id: props} table referenced per cell.
"""

import base64
import datetime
import json
import logging
import re

import requests

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

TOKEN_URL = 'https://oauth2.googleapis.com/token'
SHEETS_API = 'https://sheets.googleapis.com/v4/spreadsheets/{id}'

P_SHEETS = 'mint_schedule_sync.sheets'
P_CLIENT_ID = 'mint_schedule_sync.client_id'
P_CLIENT_SECRET = 'mint_schedule_sync.client_secret'
P_REFRESH = 'mint_schedule_sync.refresh_token'
P_BACK = 'mint_schedule_sync.back_days'
P_FORWARD = 'mint_schedule_sync.forward_days'

OSPREADSHEET_VERSION = '18.5.10'
LOCALE = {
    'name': 'English (US)', 'code': 'en_US', 'thousandsSeparator': ',',
    'decimalSeparator': '.', 'dateFormat': 'm/d/yyyy',
    'timeFormat': 'hh:mm:ss a', 'formulaArgSeparator': ',',
}

DATE_RE = re.compile(r'\b(\d{1,2})/(\d{1,2})/(\d{4})\b')

# Fetching cell formatting needs includeGridData, which is far heavier than
# values. Only worth it for a normal week window.
STYLE_TAB_LIMIT = 25


def col_letter(idx):
    s, idx = '', idx + 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        s = chr(65 + rem) + s
    return s


def a1(row, col):
    return '%s%d' % (col_letter(col), row + 1)


def quote_tab(title):
    return "'" + title.replace("'", "''") + "'"


def gcolor(c):
    """Sheets colour {red,green,blue} floats -> '#RRGGBB'."""
    if not isinstance(c, dict):
        return None
    r, g, b = (int(round(255 * float(c.get(k, 0)))) for k in ('red', 'green', 'blue'))
    return '#%02X%02X%02X' % (r, g, b)


class MintScheduleSync(models.AbstractModel):
    _name = 'mint.schedule.sync'
    _description = 'Mint Store Schedule Sync'

    # ------------------------------------------------------------------ auth
    @api.model
    def _access_token(self):
        icp = self.env['ir.config_parameter'].sudo()
        cid = icp.get_param(P_CLIENT_ID)
        secret = icp.get_param(P_CLIENT_SECRET)
        refresh = icp.get_param(P_REFRESH)
        if not (cid and secret and refresh):
            raise ValueError(
                'Missing Google credentials — set %s, %s and %s'
                % (P_CLIENT_ID, P_CLIENT_SECRET, P_REFRESH))
        resp = requests.post(TOKEN_URL, timeout=60, data={
            'client_id': cid, 'client_secret': secret,
            'refresh_token': refresh, 'grant_type': 'refresh_token',
        })
        if resp.status_code != 200:
            raise ValueError('Google token refresh failed: HTTP %s %s'
                             % (resp.status_code, resp.text[:200]))
        return resp.json()['access_token']

    # ------------------------------------------------------------------ fetch
    @api.model
    def _batch_get(self, sheet_id, ranges, token, chunk=40):
        out = {}
        for i in range(0, len(ranges), chunk):
            part = ranges[i:i + chunk]
            resp = requests.get(
                SHEETS_API.format(id=sheet_id) + '/values:batchGet',
                params=[('ranges', x) for x in part] +
                       [('majorDimension', 'ROWS'),
                        ('valueRenderOption', 'FORMATTED_VALUE')],
                headers={'Authorization': 'Bearer %s' % token}, timeout=180)
            if resp.status_code != 200:
                raise ValueError('batchGet failed: HTTP %s %s'
                                 % (resp.status_code, resp.text[:200]))
            for rng, vr in zip(part, resp.json().get('valueRanges', [])):
                out[rng] = vr.get('values', [])
        return out

    @api.model
    def _dates_in(self, grid, scan_rows=10):
        found = set()
        for row in grid[:scan_rows]:
            for cell in row:
                for m in DATE_RE.finditer(cell or ''):
                    mo, d, y = (int(x) for x in m.groups())
                    try:
                        found.add(datetime.date(y, mo, d))
                    except ValueError:
                        pass
        return sorted(found)

    @api.model
    def _select_weeks(self, stubs, back_days, forward_days):
        """Every tab whose week falls in the window.

        Deliberately not de-duplicated on week start: Tempe splits one week
        across `PSR 8/17-8/23` and `Non PSR 8/17-8/23`, so collapsing them
        drops a whole staff group silently.
        """
        today = fields.Date.context_today(self)
        lo = today - datetime.timedelta(days=back_days)
        hi = today + datetime.timedelta(days=forward_days)
        dated = [t for t in stubs if t['week_start']]
        hits = [t for t in dated if lo <= t['week_start'] <= hi]
        if hits:
            return sorted(hits, key=lambda t: (t['week_start'], t['title']))
        dated.sort(key=lambda t: t['week_start'])
        return dated[-2:]

    @api.model
    def _read_sheet(self, sheet_id, token, back_days, forward_days):
        meta = requests.get(
            SHEETS_API.format(id=sheet_id),
            params={'fields': 'sheets(properties(title,sheetId,gridProperties),merges)'},
            headers={'Authorization': 'Bearer %s' % token}, timeout=120)
        if meta.status_code != 200:
            raise ValueError('sheets.get failed: HTTP %s %s'
                             % (meta.status_code, meta.text[:200]))
        sheets_meta = meta.json().get('sheets', [])
        titles = [s['properties']['title'] for s in sheets_meta]

        head_ranges = ['%s!A1:BZ10' % quote_tab(t) for t in titles]
        heads = self._batch_get(sheet_id, head_ranges, token)

        stubs = []
        for title, rng in zip(titles, head_ranges):
            grid = [[('' if c is None else str(c)) for c in row]
                    for row in heads.get(rng, [])]
            dates = self._dates_in(grid)
            stubs.append({'title': title, 'grid': [], 'merges': [], 'styles': {},
                          'week_start': dates[0] if dates else None})

        picked = self._select_weeks(stubs, back_days, forward_days)
        if not picked:
            return []

        by_title = {s['properties']['title']: s for s in sheets_meta}
        ranges = []
        for t in picked:
            gp = by_title.get(t['title'], {}).get('properties', {}).get(
                'gridProperties', {})
            end = a1(max(gp.get('rowCount', 500), 1) - 1,
                     max(gp.get('columnCount', 60), 1) - 1)
            ranges.append('%s!A1:%s' % (quote_tab(t['title']), end))
        full = self._batch_get(sheet_id, ranges, token)

        for t, rng in zip(picked, ranges):
            grid = [[('' if c is None else str(c)) for c in row]
                    for row in full.get(rng, [])]
            while grid and not any(c.strip() for c in grid[-1]):
                grid.pop()
            width = max([len(r) for r in grid] or [0])
            t['grid'] = [r + [''] * (width - len(r)) for r in grid]
            t['merges'] = [
                '%s:%s' % (a1(m.get('startRowIndex', 0), m.get('startColumnIndex', 0)),
                           a1(m.get('endRowIndex', 1) - 1, m.get('endColumnIndex', 1) - 1))
                for m in (by_title.get(t['title'], {}).get('merges') or [])
            ]

        if len(picked) <= STYLE_TAB_LIMIT:
            try:
                self._attach_styles(sheet_id, picked, ranges, token)
            except Exception:
                _logger.warning('styles unavailable for %s', sheet_id, exc_info=True)
        return picked

    @api.model
    def _attach_styles(self, sheet_id, picked, ranges, token):
        flds = ('sheets(data(rowData(values(effectiveFormat('
                'backgroundColor,textFormat)))))')
        resp = requests.get(
            SHEETS_API.format(id=sheet_id),
            params=[('ranges', x) for x in ranges] +
                   [('includeGridData', 'true'), ('fields', flds)],
            headers={'Authorization': 'Bearer %s' % token}, timeout=300)
        if resp.status_code != 200:
            raise ValueError('grid data failed: HTTP %s' % resp.status_code)
        for tab, sh in zip(picked, resp.json().get('sheets', [])):
            styles = {}
            data = (sh.get('data') or [{}])[0]
            for ri, row in enumerate(data.get('rowData') or []):
                for ci, cell in enumerate(row.get('values') or []):
                    ef = cell.get('effectiveFormat') or {}
                    props = {}
                    bg = gcolor(ef.get('backgroundColor'))
                    if bg and bg != '#FFFFFF':
                        props['fillColor'] = bg
                    tf = ef.get('textFormat') or {}
                    if tf.get('bold'):
                        props['bold'] = True
                    if tf.get('italic'):
                        props['italic'] = True
                    fg = gcolor(tf.get('foregroundColor'))
                    if fg and fg != '#000000':
                        props['textColor'] = fg
                    if (props and ri < len(tab['grid'])
                            and ci < len(tab['grid'][ri])
                            and str(tab['grid'][ri][ci]).strip()):
                        styles[(ri, ci)] = props
            tab['styles'] = styles

    # ------------------------------------------------------------------ build
    @api.model
    def _build_sheet(self, tab, index, style_table):
        cells = {}
        max_cols = 0
        for r, row in enumerate(tab['grid']):
            max_cols = max(max_cols, len(row))
            for c, val in enumerate(row):
                text = ('' if val is None else str(val)).strip()
                if text:
                    cells[a1(r, c)] = text

        sheet_styles = {}
        for (r, c), props in (tab.get('styles') or {}).items():
            key = json.dumps(props, sort_keys=True)
            sid = style_table.get(key)
            if sid is None:
                sid = len(style_table) + 1
                style_table[key] = sid
            sheet_styles[a1(r, c)] = sid

        safe = re.sub(r'[^A-Za-z0-9]', '_', tab['title']).strip('_').lower()
        return {
            'id': ('wk_%03d_%s' % (index, safe))[:60] or 'wk_%03d' % index,
            'name': tab['title'][:100],
            'cells': cells,
            'colNumber': max(max_cols, 1),
            'rowNumber': max(len(tab['grid']), 1),
            'cols': {'0': {'size': 150}},
            'rows': {},
            'merges': list(tab.get('merges') or []),
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

    @api.model
    def _build_document(self, sheets, style_table):
        return {
            'version': OSPREADSHEET_VERSION,
            'revisionId': 'START_REVISION',
            'sheets': sheets,
            'styles': {str(v): json.loads(k) for k, v in style_table.items()},
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

    # -------------------------------------------------------------------- run
    @api.model
    def _sync_store(self, store, token, back_days, forward_days):
        picked = self._read_sheet(store['sheet_id'], token, back_days, forward_days)
        if not picked:
            _logger.warning('schedule sync: no weeks matched for %s', store['store'])
            return None

        style_table = {}
        sheets = [self._build_sheet(t, i, style_table) for i, t in enumerate(picked)]
        doc = self._build_document(sheets, style_table)
        payload = base64.b64encode(json.dumps(doc).encode('utf-8')).decode('ascii')

        name = 'Schedule — %s' % store['store']
        Spreadsheet = self.env['spreadsheet.spreadsheet'].sudo()
        existing = Spreadsheet.search([('name', '=', name)], limit=1)
        vals = {'spreadsheet_binary_data': payload}
        if store.get('company'):
            company = self.env['res.company'].sudo().search(
                [('name', '=', store['company'])], limit=1)
            if company:
                vals['company_id'] = company.id
        if existing:
            existing.write(vals)
            rec = existing
        else:
            vals['name'] = name
            rec = Spreadsheet.create(vals)
        _logger.info('schedule sync: %s -> id=%s, %d tab(s), %d cells',
                     store['store'], rec.id, len(sheets),
                     sum(len(s['cells']) for s in sheets))
        return rec.id

    @api.model
    def cron_sync_schedules(self):
        """Entry point for ir.cron."""
        icp = self.env['ir.config_parameter'].sudo()
        raw = icp.get_param(P_SHEETS)
        if not raw:
            _logger.warning('schedule sync: %s not configured, nothing to do',
                            P_SHEETS)
            return
        stores = json.loads(raw)
        back = int(icp.get_param(P_BACK) or 7)
        forward = int(icp.get_param(P_FORWARD) or 14)

        token = self._access_token()
        ok = failed = 0
        for store in stores:
            try:
                self._sync_store(store, token, back, forward)
                ok += 1
            except Exception:
                failed += 1
                # one unreachable sheet must not abort the rest
                _logger.exception('schedule sync failed for %s',
                                  store.get('store'))
        _logger.info('schedule sync finished: %d ok, %d failed', ok, failed)
