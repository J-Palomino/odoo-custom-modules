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


def read_via_api(sheet_id, token, want_all=False, **week_kw):
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

    # full pass: only the tabs we are actually going to sync
    full_ranges = [f'{_quote_tab(t["title"])}!A1:BZ200' for t in picked]
    full = _batch_get(sheet_id, full_ranges, token)

    by_title = {s['properties']['title']: s for s in sheets_meta}
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
    return picked


def _merges_for(ws):
    """openpyxl merged ranges are already A1 zone strings ("B1:C1")."""
    return [str(m) for m in ws.merged_cells.ranges]


def read_workbook(path):
    """-> [ {title, grid, merges, dates, week_start} ] in workbook tab order."""
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True, read_only=False)
    tabs = []
    for ws in wb.worksheets:
        grid = []
        for row in ws.iter_rows(values_only=True):
            grid.append(['' if c is None else _fmt(c) for c in row])
        # trim trailing fully-empty rows
        while grid and not any(c.strip() for c in grid[-1]):
            grid.pop()
        dates = _dates_in(grid)
        tabs.append({
            'title': ws.title,
            'grid': grid,
            'merges': _merges_for(ws),
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
