"""Pull a Google Sheets workbook down and hand back plain 2-D week grids.

Deliberately reads the workbook as XLSX via the Drive *export* endpoint rather
than the Sheets API: export needs only a Drive scope, which is what both the
service account and a `gcloud auth login --enable-gdrive-access` credential
actually carry. XLSX also preserves the merged ranges these schedules rely on.

Credential resolution order:
  1. the OAuth refresh token stored in Odoo by oauth_setup.py — preferred, and
     the only option that is both unattended and tied to a human who can
     already see the sheets
  2. GOOGLE_SERVICE_ACCOUNT_JSON (path to a key file)
  3. ~/gbp-metrics-key.json                      — needs the sheet shared to it
  4. `gcloud auth print-access-token`            — whoever gcloud is logged in as
"""

import datetime as dt
import os
import re
import subprocess

import requests

from config import SCOPES, SERVICE_ACCOUNT_KEY

EXPORT_URL = 'https://www.googleapis.com/drive/v3/files/{id}/export'
XLSX_MIME = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'

DATE_RE = re.compile(r'\b(\d{1,2})/(\d{1,2})/(\d{4})\b')


class AuthError(RuntimeError):
    pass


class ExportTooLarge(RuntimeError):
    """Drive won't export this workbook — use the Sheets API instead."""


def _sa_token(key_path):
    from google.oauth2 import service_account
    import google.auth.transport.requests as gt
    creds = service_account.Credentials.from_service_account_file(
        key_path, scopes=SCOPES)
    creds.refresh(gt.Request())
    return creds.token


def _gcloud_token():
    try:
        out = subprocess.run(['gcloud', 'auth', 'print-access-token'],
                             capture_output=True, text=True, timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() or None


def _odoo_oauth_token():
    """Exchange the refresh token stored in Odoo for an access token."""
    try:
        from odoo import Odoo
        import oauth_setup as osu
    except ImportError:
        return None, None

    try:
        o = Odoo()
        refresh = osu.get_param(o, osu.REFRESH_KEY)
        if not refresh:
            return None, None
        client_id = osu.get_param(o, osu.CLIENT_ID_KEY)
        client_secret = osu.get_param(o, osu.CLIENT_SECRET_KEY)
        account = osu.get_param(o, osu.ACCOUNT_KEY) or 'stored account'
        r = requests.post(osu.TOKEN_URL, timeout=60, data={
            'client_id': client_id,
            'client_secret': client_secret,
            'refresh_token': refresh,
            'grant_type': 'refresh_token',
        })
        if r.status_code != 200:
            return None, None
        return r.json()['access_token'], f'Odoo OAuth token ({account})'
    except Exception:
        return None, None


def can_read(token, file_id):
    """A credential that exists is not necessarily a credential that works —
    the service account authenticates fine but cannot see these files, so every
    candidate is probed against a real file before being accepted."""
    if not token or not file_id:
        return False
    try:
        r = requests.get(
            f'https://www.googleapis.com/drive/v3/files/{file_id}',
            params={'fields': 'id'},
            headers={'Authorization': f'Bearer {token}'}, timeout=30)
        return r.status_code == 200
    except Exception:
        return False


def _candidates():
    """Yield (token, description) in preference order, skipping ones that
    cannot even be minted."""
    tok, how = _odoo_oauth_token()
    if tok:
        yield tok, how

    explicit = os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON')
    if explicit and os.path.exists(explicit):
        try:
            yield _sa_token(explicit), f'service account ({explicit})'
        except Exception:
            pass

    key = os.path.expanduser(SERVICE_ACCOUNT_KEY)
    if os.path.exists(key):
        try:
            yield _sa_token(key), f'service account ({SERVICE_ACCOUNT_KEY})'
        except Exception:
            pass

    tok = _gcloud_token()
    if tok:
        yield tok, 'gcloud user credential'


def get_token(probe_file_ids=None):
    """Return (token, description) or raise AuthError with what to do next.

    Every candidate must be able to read *all* the files we intend to sync, not
    just the first. Sharing differs per sheet — the service account can read
    Tempe but not Mesa — so probing a single file picks a credential that then
    fails partway through the run.
    """
    probe = [p for p in (probe_file_ids or []) if p]
    rejected = []
    for tok, how in _candidates():
        if not probe:
            return tok, how
        missing = [p for p in probe if not can_read(tok, p)]
        if not missing:
            return tok, how
        rejected.append(f'{how} — cannot read {len(missing)}/{len(probe)} sheets')

    if rejected:
        raise AuthError(
            'Found Google credentials but none can read every sheet:\n' +
            ''.join(f'  - {r}\n' for r in rejected) +
            '  Fix: gcloud auth login --enable-gdrive-access, or share the\n'
            '  sheets with the service account.')

    raise AuthError(
        'No usable Google credential.\n'
        '  Preferred: python3 oauth_setup.py   (one-time browser consent,\n'
        '  reuses the OAuth client already configured in Odoo)\n'
        '  Alternatives: share the 8 sheets with the service account, or run\n'
        '    gcloud auth login --enable-gdrive-access')


def download_xlsx(sheet_id, dest, token=None):
    """Export a Google Sheet to XLSX on disk. Returns dest."""
    if token is None:
        token, _ = get_token()
    r = requests.get(EXPORT_URL.format(id=sheet_id),
                     params={'mimeType': XLSX_MIME},
                     headers={'Authorization': f'Bearer {token}'},
                     stream=True, timeout=180)
    if r.status_code != 200:
        body = r.text
        if r.status_code == 403 and ('exportSizeLimitExceeded' in body
                                     or 'too large to be exported' in body):
            raise ExportTooLarge(
                f'{sheet_id} exceeds the Drive export size limit')
        raise RuntimeError(
            f'export failed for {sheet_id}: HTTP {r.status_code} '
            f'{body[:200]}')
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, 'wb') as fh:
        for chunk in r.iter_content(65536):
            fh.write(chunk)
    return dest


SHEETS_API = 'https://sheets.googleapis.com/v4/spreadsheets/{id}'


def _a1(row, col):
    s, idx = '', col + 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        s = chr(65 + rem) + s
    return f'{s}{row + 1}'


def _quote_tab(title):
    return "'" + title.replace("'", "''") + "'"


def _batch_get(sheet_id, ranges, token, chunk=40):
    """values.batchGet, chunked so the URL stays sane on 176-tab workbooks."""
    out = {}
    for i in range(0, len(ranges), chunk):
        part = ranges[i:i + chunk]
        r = requests.get(
            SHEETS_API.format(id=sheet_id) + '/values:batchGet',
            params=[('ranges', x) for x in part] +
                   [('majorDimension', 'ROWS'),
                    ('valueRenderOption', 'FORMATTED_VALUE')],
            headers={'Authorization': f'Bearer {token}'}, timeout=180)
        if r.status_code != 200:
            raise RuntimeError(f'batchGet failed: HTTP {r.status_code} '
                               f'{r.text[:200]}')
        for rng, vr in zip(part, r.json().get('valueRanges', [])):
            out[rng] = vr.get('values', [])
    return out


def read_via_api(sheet_id, token, want_all=False, style_tab_limit=25, **week_kw):
    """Read a workbook through the Sheets API instead of an XLSX export.

    Needed for workbooks Drive refuses to export (`exportSizeLimitExceeded` —
    Tempe carries 176 weeks). Also cheaper, since only the selected weeks are
    fetched in full.
    """
    meta = requests.get(
        SHEETS_API.format(id=sheet_id),
        params={'fields': 'sheets(properties(title,sheetId,gridProperties),merges)'},
        headers={'Authorization': f'Bearer {token}'}, timeout=120)
    if meta.status_code != 200:
        raise RuntimeError(f'sheets.get failed: HTTP {meta.status_code} '
                           f'{meta.text[:200]}')
    sheets_meta = meta.json().get('sheets', [])
    titles = [s['properties']['title'] for s in sheets_meta]

    # cheap pass: just the top rows of every tab, to date-stamp each week
    head_ranges = [f'{_quote_tab(t)}!A1:BZ10' for t in titles]
    heads = _batch_get(sheet_id, head_ranges, token)

    stubs = []
    for t, rng in zip(titles, head_ranges):
        grid = [[('' if c is None else str(c)) for c in row]
                for row in heads.get(rng, [])]
        dates = _dates_in(grid)
        stubs.append({'title': t, 'grid': [], 'merges': [], 'dates': dates,
                      'week_start': min(dates) if dates else None})

    picked = stubs if want_all else select_weeks(stubs, **week_kw)
    if not picked:
        return []

    # full pass: only the tabs we are actually going to sync.
    # Range comes from each tab's real gridProperties rather than a fixed
    # A1:BZ200 — a hardcoded cap silently truncates any larger tab.
    by_title = {s['properties']['title']: s for s in sheets_meta}
    full_ranges_meta = []
    full_ranges = []
    for t in picked:
        gp = (by_title.get(t['title'], {}).get('properties', {})
              .get('gridProperties', {}))
        rows = gp.get('rowCount', 500)
        cols = gp.get('columnCount', 60)
        end = _a1(max(rows, 1) - 1, max(cols, 1) - 1)
        full_ranges.append(f'{_quote_tab(t["title"])}!A1:{end}')
    full = _batch_get(sheet_id, full_ranges, token)
    for t, rng in zip(picked, full_ranges):
        grid = [[('' if c is None else str(c)) for c in row]
                for row in full.get(rng, [])]
        while grid and not any(c.strip() for c in grid[-1]):
            grid.pop()
        width = max((len(r) for r in grid), default=0)
        t['grid'] = [r + [''] * (width - len(r)) for r in grid]
        t['merges'] = [
            f"{_a1(m.get('startRowIndex', 0), m.get('startColumnIndex', 0))}:"
            f"{_a1(m.get('endRowIndex', 1) - 1, m.get('endColumnIndex', 1) - 1)}"
            for m in (by_title.get(t['title'], {}).get('merges') or [])
        ]
        t['styles'] = {}

    # Formatting needs includeGridData, which is far heavier than values — only
    # worth it for a normal week window, not a 225-tab archive.
    if len(picked) <= style_tab_limit:
        try:
            _attach_api_styles(sheet_id, picked, full_ranges, token)
        except Exception as e:
            print(f'  (styles unavailable via API: {type(e).__name__})')
    return picked


def _gc(c):
    """Sheets colour {red,green,blue} floats -> '#RRGGBB'."""
    if not isinstance(c, dict):
        return None
    r, g, b = (int(round(255 * float(c.get(k, 0)))) for k in ('red', 'green', 'blue'))
    return f'#{r:02X}{g:02X}{b:02X}'


def _attach_api_styles(sheet_id, picked, ranges, token):
    """Populate tab['styles'] from effectiveFormat for the selected tabs."""
    fields = ('sheets(data(rowData(values(effectiveFormat('
              'backgroundColor,textFormat)))))')
    r = requests.get(
        SHEETS_API.format(id=sheet_id),
        params=[('ranges', x) for x in ranges] +
               [('includeGridData', 'true'), ('fields', fields)],
        headers={'Authorization': f'Bearer {token}'}, timeout=300)
    if r.status_code != 200:
        raise RuntimeError(f'HTTP {r.status_code} {r.text[:150]}')
    got = r.json().get('sheets', [])
    for tab, sh in zip(picked, got):
        styles = {}
        data = (sh.get('data') or [{}])[0]
        for ri, row in enumerate(data.get('rowData') or []):
            for ci, cell in enumerate(row.get('values') or []):
                ef = cell.get('effectiveFormat') or {}
                props = {}
                bg = _gc(ef.get('backgroundColor'))
                if bg and bg != '#FFFFFF':
                    props['fillColor'] = bg
                tf = ef.get('textFormat') or {}
                if tf.get('bold'):
                    props['bold'] = True
                if tf.get('italic'):
                    props['italic'] = True
                fg = _gc(tf.get('foregroundColor'))
                if fg and fg != '#000000':
                    props['textColor'] = fg
                if props and ri < len(tab['grid']) \
                        and ci < len(tab['grid'][ri]) \
                        and str(tab['grid'][ri][ci]).strip():
                    styles[(ri, ci)] = props
        tab['styles'] = styles


def _merges_for(ws):
    """openpyxl merged ranges are already A1 zone strings ("B1:C1")."""
    return [str(m) for m in ws.merged_cells.ranges]


def _argb_to_hex(v):
    """openpyxl gives ARGB ('FFB6D7A8'); o-spreadsheet wants '#B6D7A8'.
    Theme/indexed colours come back as non-strings and are skipped."""
    if not isinstance(v, str) or len(v) not in (6, 8):
        return None
    rgb = v[-6:].upper()
    if rgb in ('FFFFFF', '000000') and len(v) == 8 and v[:2] == '00':
        return None
    return '#' + rgb


def _cell_style(c):
    """Extract the styling that carries meaning on these schedules."""
    props = {}
    try:
        f = c.fill
        if f is not None and getattr(f, 'patternType', None) and f.fgColor is not None:
            hexv = _argb_to_hex(getattr(f.fgColor, 'rgb', None))
            # white fill is the default ground; storing it adds noise only
            if hexv and hexv != '#FFFFFF':
                props['fillColor'] = hexv
    except Exception:
        pass
    try:
        fo = c.font
        if fo is not None:
            if fo.bold:
                props['bold'] = True
            if fo.italic:
                props['italic'] = True
            if fo.color is not None:
                hexv = _argb_to_hex(getattr(fo.color, 'rgb', None))
                if hexv and hexv != '#000000':
                    props['textColor'] = hexv
    except Exception:
        pass
    return props


def read_workbook(path, with_styles=True):
    """-> [ {title, grid, merges, styles, dates, week_start} ] in tab order.

    `styles` maps (row, col) -> o-spreadsheet style props. These schedules use
    fill colour to encode meaning (Cave Creek's current week alone carries four
    fills across ~400 cells), so dropping it loses information the grid does
    not otherwise carry.
    """
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True, read_only=False)
    tabs = []
    for ws in wb.worksheets:
        grid = []
        styles = {}
        for r, row in enumerate(ws.iter_rows()):
            vals = []
            for c, cell in enumerate(row):
                v = cell.value
                vals.append('' if v is None else _fmt(v))
                if with_styles and v is not None and str(v).strip():
                    props = _cell_style(cell)
                    if props:
                        styles[(r, c)] = props
            grid.append(vals)
        # trim trailing fully-empty rows
        while grid and not any(c.strip() for c in grid[-1]):
            grid.pop()
        styles = {k: v for k, v in styles.items() if k[0] < len(grid)}
        dates = _dates_in(grid)
        tabs.append({
            'title': ws.title,
            'grid': grid,
            'merges': _merges_for(ws),
            'styles': styles,
            'dates': dates,
            'week_start': min(dates) if dates else None,
        })
    wb.close()
    return tabs


def _fmt(v):
    """Render a cell the way the sheet shows it, not the way Python repr's it."""
    if isinstance(v, dt.datetime):
        # Times come back as datetimes on a 1899/1900 epoch date.
        if v.year <= 1900:
            return v.strftime('%-I:%M:%S %p')
        return v.strftime('%-m/%-d/%Y')
    if isinstance(v, dt.time):
        return v.strftime('%-I:%M:%S %p')
    if isinstance(v, dt.date):
        return v.strftime('%-m/%-d/%Y')
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _dates_in(grid, scan_rows=10):
    """Collect M/D/YYYY dates from the top of a tab — the only reliable way to
    identify which week a tab covers (tab titles are inconsistent and, for two
    workbooks, duplicated)."""
    found = []
    for row in grid[:scan_rows]:
        for cell in row:
            for m in DATE_RE.finditer(cell):
                mo, d, y = (int(x) for x in m.groups())
                try:
                    found.append(dt.date(y, mo, d))
                except ValueError:
                    pass
    return sorted(set(found))


def select_weeks(tabs, today=None, back_days=7, forward_days=14, fallback=2):
    """Pick the tabs worth syncing: every tab whose week overlaps a window
    around today. Falls back to the last N dated tabs when nothing is in range.

    Deliberately does NOT de-duplicate on week_start. Two tabs sharing a week
    are usually complementary rather than redundant — Tempe splits a single week
    across `PSR 8/17-8/23` and `Non PSR 8/17-8/23`, so collapsing them drops an
    entire staff group without any error. Importing a redundant tab is visible
    and harmless; losing people is neither.
    """
    today = today or dt.date.today()
    lo = today - dt.timedelta(days=back_days)
    hi = today + dt.timedelta(days=forward_days)

    dated = [t for t in tabs if t['week_start']]
    hits = [t for t in dated if lo <= t['week_start'] <= hi]
    if hits:
        return sorted(hits, key=lambda t: (t['week_start'], t['title']))

    dated.sort(key=lambda t: t['week_start'])
    return dated[-fallback:]
