# -*- coding: utf-8 -*-
"""Parse shifts out of the schedule spreadsheets already stored in Odoo.

Layout-agnostic by construction. The stores use four different grid layouts and
the date row sits at irregular column offsets (merged cells make Mesa stride
2,4,2,4...), so nothing is read at a fixed offset. Instead:

  * the day columns come from the date row
  * within one day's column span, cells that parse as a clock time are taken in
    order: first = start, second = end
  * anything else in that span (hours totals, role tags, OFF, notes) is ignored
    because it is not a time

Correctness is checked against the sheet's own hours column where it has one:
if (end - start) matches the stated hours, the columns were read correctly.
"""
import datetime as dt
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from odoo import Odoo, col_letter

DATE_RE = re.compile(r'^(\d{1,2})/(\d{1,2})/(\d{4})')
TIME_RE = re.compile(r'^(\d{1,2}):(\d{2})(?::(\d{2}))?\s*([AaPp])\.?[Mm]\.?$')
NUM_RE = re.compile(r'^\d+(\.\d+)?$')
# Signed/spreadsheet-error values also occupy the totals columns and must not
# be mistaken for names — El Mirage column A is full of values like "-3.75".
NOTNAME_RE = re.compile(r'^[-+]?\d+(\.\d+)?%?$|^#[A-Z]+[!?]$')

# Cell values that occupy a shift slot but are not a shift.
NOT_A_SHIFT = {
    'off', 'r/off', 'b/off', 'req off', 'requested off', 'request off',
    'pto', 'on call', 'dt', 'bell', 'loa', 'leave', 'sick', 'vacation',
    'holiday', 'training', 'onboarding', 'audit', 'tbd', 'open', 'new hire',
    'overstock', 'mia', 'ncns', 'no call no show', 'cust appr', 'n/a', '',
}

NOT_A_NAME = {
    'admin', 'administration', 'am shift', 'pm shift', 'mid shift', 'flex',
    'flex/mid', 'boh', 'boh clerk', 'foh', 'leads', 'lead', 'leadership',
    'inventory', 'management', 'psr', 'psrs', 'shift captains', 'captn',
    'drive thru support', 'totals', 'total', 'needed', 'wages', 'sales',
    'labor', 'labor %', 'hours', 'begin', 'end', 'start', 'subject to change',
    'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday',
    'sunday', 'name', 'employee', 'week', 'notes', 'note', '-', 'open',
    'new hire', 'nocturnal nugs', 'early birds', 'hybrid heroes',
}


def parse_time(v):
    m = TIME_RE.match((v or '').strip())
    if not m:
        return None
    h, mi, _, ap = m.groups()
    h, mi = int(h), int(mi)
    if h == 12:
        h = 0
    if ap.lower() == 'p':
        h += 12
    return dt.time(h % 24, mi)


def norm(s):
    return re.sub(r'\s+', ' ', re.sub(r'[^A-Za-z ]', ' ', s or '')).strip().lower()


def parse_tab(sheet):
    """-> (shifts, stats). shifts = [{name,row,date,start,end,hours,stated}]"""
    cells = sheet['cells']
    ncol, nrow = sheet['colNumber'], sheet['rowNumber']
    get = lambda r, c: cells.get(f'{col_letter(c)}{r + 1}', '')

    # 1. date row: the early row carrying the most real dates
    drow, dn = None, 0
    for r in range(min(10, nrow)):
        n = sum(1 for c in range(ncol) if DATE_RE.match(get(r, c)))
        if n > dn:
            drow, dn = r, n
    if drow is None or dn < 3:
        return [], {'reason': 'no date row'}

    daycols = []
    for c in range(ncol):
        m = DATE_RE.match(get(drow, c))
        if m:
            mo, d, y = (int(x) for x in m.groups())
            try:
                daycols.append((c, dt.date(y, mo, d)))
            except ValueError:
                pass
    # column span belonging to each day
    spans = []
    for i, (c, day) in enumerate(daycols):
        end = daycols[i + 1][0] if i + 1 < len(daycols) else ncol
        spans.append((day, c, end))

    # 2. name column: whichever of the first three holds the most name-ish text
    namecol, best = 0, -1
    # Only the leading label columns are candidates. Column 3 is already a
    # day column in layout D and its "OFF" values would win on volume.
    for c in (0, 1, 2):
        n = 0
        for r in range(drow + 1, nrow):
            v = get(r, c).strip()
            if v and not NOTNAME_RE.match(v) and not parse_time(v) \
               and norm(v) not in NOT_A_NAME and 1 < len(v) < 40:
                n += 1
        if n > best:
            namecol, best = c, n

    shifts, incomplete, skipped = [], 0, 0
    for r in range(drow + 1, nrow):
        raw = get(r, namecol).strip()
        if not raw or norm(raw) in NOT_A_NAME or NOTNAME_RE.match(raw):
            continue
        for day, c0, c1 in spans:
            times, endcol = [], None
            # never read left of the name column: layout D puts the heatmap's
            # hour-of-day axis there, a valid time on every single row
            for c in range(max(c0, namecol + 1), c1):
                v = get(r, c).strip()
                t = parse_time(v)
                if t:
                    times.append(t)
                    if len(times) == 2:
                        endcol = c
            # The hours cell sits immediately right of the end time, which for
            # merged layouts can fall outside this day's span — so read it by
            # position relative to the end time, not by span membership.
            # Layouts differ in where the hours figure sits relative to the end
            # time: immediately right in the triple layouts, four columns right
            # in the 45-col hybrid (SCHEDULED/NEEDED/Variance then Daily Hours).
            # Collect the candidates and let the caller compare.
            stated = []
            if endcol is not None:
                for c in range(endcol + 1, min(endcol + 5, ncol)):
                    v = get(r, c).strip()
                    if NUM_RE.match(v):
                        stated.append(float(v))
            if not times:
                skipped += 1
                continue
            if len(times) == 1:
                incomplete += 1
                continue
            start, end = times[0], times[1]
            dur = ((dt.datetime.combine(day, end) -
                    dt.datetime.combine(day, start)).total_seconds() / 3600)
            if dur <= 0:                    # crosses midnight
                dur += 24
            shifts.append({'name': raw, 'row': r + 1, 'date': day,
                           'start': start, 'end': end,
                           'hours': round(dur, 2), 'stated': stated})
    return shifts, {'incomplete': incomplete, 'empty_slots': skipped,
                    'date_row': drow + 1, 'name_col': col_letter(namecol),
                    'days': len(spans)}


def load(store):
    o = Odoo()
    rec = o.execute('spreadsheet.spreadsheet', 'search_read',
                    [['name', '=', f'Schedule — {store}']],
                    fields=['spreadsheet_raw'], limit=1)
    if not rec:
        return []
    raw = rec[0]['spreadsheet_raw']
    if isinstance(raw, str):
        raw = json.loads(raw)
    return raw['sheets']


if __name__ == '__main__':
    for store in sys.argv[1:] or ['Mesa']:
        print(f"\n===== {store} =====")
        for sheet in load(store):
            shifts, st = parse_tab(sheet)
            if not shifts:
                print(f"  tab '{sheet['name']}': {st}")
                continue
            # verification: does end-start agree with the sheet's own hours?
            checked = [s for s in shifts if s['stated']]
            # The sheets state PAID hours, which deduct an unpaid break, so an
            # exact match is not expected — anything from equal to 1h short is
            # consistent with a correctly read start/end pair.
            agree = [s for s in checked
                     if any(-0.05 <= s['hours'] - v <= 1.05 for v in s['stated'])]
            pct = (100 * len(agree) // len(checked)) if checked else -1
            odd = [s for s in shifts if s['hours'] > 14 or s['hours'] < 1]
            print(f"  tab '{sheet['name'][:26]:<26} {len(shifts):>4} shifts | "
                  f"dates row {st['date_row']}, names col {st['name_col']} | "
                  f"hours cross-check {len(agree)}/{len(checked)}"
                  f"{f' ({pct}%)' if checked else ' (no hours col)'}"
                  f" | incomplete {st['incomplete']} | implausible {len(odd)}")
            for s in shifts[:3]:
                print(f"      {s['name'][:16]:<16} {s['date']} "
                      f"{s['start'].strftime('%H:%M')}-{s['end'].strftime('%H:%M')} "
                      f"= {s['hours']}h (sheet says {s['stated']})")
