# -*- coding: utf-8 -*-
"""
Publish the MINTLE word schedule from an Odoo spreadsheet into mint.config.

The workbook "MINTLE Schedule" is the source of truth for the daily word game.
A cron reads it and writes the `mintle_schedule` config key, which the
storefront fetches from /api/v1/config/mintle_schedule.

Why this is not a two-line read
-------------------------------
`spreadsheet.spreadsheet.spreadsheet_raw` is only a BASE SNAPSHOT. Every edit a
human makes in the browser is stored separately as a `spreadsheet.oca.revision`
row of o-spreadsheet commands; `get_spreadsheet_data()` hands the base and the
revisions to the *browser*, which replays them client-side. The server never
folds them in.

So reading `spreadsheet_raw` alone returns the sheet as it was when it was last
written programmatically and silently ignores every human edit — the worst
possible failure for a source of truth, because it looks like it worked.
Verified on this database 2026-08-25: a sheet whose `write_date` still read
2026-08-20 had eight answers typed into it that appeared in no raw read.

This module therefore replays the revision chain over the base, the same way the
browser does.

Fail-safe posture
-----------------
* A command the replay does not understand aborts the read — nothing is
  published and the previous config value stands. Publishing a subtly wrong
  schedule is worse than publishing nothing.
* A single malformed ROW is skipped, not fatal: that date falls through to the
  storefront's built-in rotation, which is the documented behaviour in the
  workbook's README tab.
"""

import json
import logging
import re

from odoo import api, models

_logger = logging.getLogger(__name__)

SHEET_NAME = "MINTLE Schedule"
TAB_NAME = "Schedule"
CONFIG_KEY = "mintle_schedule"

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
WORD_RE = re.compile(r"^[A-Z]{5}$")

# Commands that only affect presentation; content is unchanged so skipping is safe.
IGNORABLE_COMMANDS = frozenset({
    "SET_FORMATTING", "CLEAR_FORMATTING", "SET_BORDER", "SET_BORDERS_ON_TARGET",
    "SET_ZONE_BORDERS", "SET_DECIMAL", "RESIZE_COLUMNS_ROWS",
    "HIDE_COLUMNS_ROWS", "UNHIDE_COLUMNS_ROWS", "FREEZE_COLUMNS", "FREEZE_ROWS",
    "UNFREEZE_COLUMNS_ROWS", "UNFREEZE_COLUMNS", "UNFREEZE_ROWS",
    "SET_GRID_LINES_VISIBILITY", "MOVE_SHEET", "CREATE_TABLE", "UPDATE_TABLE",
    "REMOVE_TABLE", "CREATE_FILTER_TABLE", "REMOVE_FILTER_TABLE",
    "UPDATE_FILTER", "CREATE_CHART", "UPDATE_CHART", "DELETE_FIGURE",
    "UPDATE_FIGURE", "CREATE_IMAGE", "SET_VIEWPORT_OFFSET", "EVALUATE_CELLS",
    "RESIZE_SHEETVIEW", "ACTIVATE_SHEET",
})


def _col_to_index(letters):
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _split_ref(ref):
    """'AB12' -> (col_index, row_index), both 0-based. None if unparseable."""
    letters, digits = "", ""
    for ch in ref:
        if ch.isalpha():
            if digits:
                return None
            letters += ch.upper()
        elif ch.isdigit():
            digits += ch
        else:
            return None
    if not letters or not digits:
        return None
    return _col_to_index(letters), int(digits) - 1


def _cell_content(value):
    """Base-snapshot cells are either a bare string or {'content': ...}."""
    if isinstance(value, dict):
        return value.get("content")
    return value


def _load_base(data):
    grids, names = {}, {}
    for sheet in (data or {}).get("sheets", []) or []:
        sid = sheet.get("id")
        if not sid:
            continue
        names[sid] = sheet.get("name") or sid
        grid = {}
        for ref, raw_value in (sheet.get("cells") or {}).items():
            pos = _split_ref(ref)
            if pos is None:
                continue
            content = _cell_content(raw_value)
            if content not in (None, ""):
                grid[pos] = content
        grids[sid] = grid
    return grids, names


def _zones(cmd):
    if cmd.get("target"):
        return cmd["target"]
    if cmd.get("zone"):
        return [cmd["zone"]]
    return []


def _clear_zones(grid, zones):
    for z in zones:
        for col in range(int(z.get("left", 0)), int(z.get("right", 0)) + 1):
            for row in range(int(z.get("top", 0)), int(z.get("bottom", 0)) + 1):
                grid.pop((col, row), None)


def _shift(grid, dimension, base, quantity, removing=False):
    """Insert or delete whole rows/columns, moving the cells after them."""
    is_row = dimension == "ROW"
    moved = {}
    for (col, row), content in grid.items():
        axis = row if is_row else col
        if removing:
            if base <= axis < base + quantity:
                continue
            new_axis = axis - quantity if axis >= base + quantity else axis
        else:
            new_axis = axis + quantity if axis >= base else axis
        moved[(col, new_axis) if is_row else (new_axis, row)] = content
    grid.clear()
    grid.update(moved)


def _order_revisions(base_revision_id, revisions):
    """
    Walk the chain: each revision names the revision it applies to
    (server_revision_id) and the one it produces (next_revision_id).

    A fork — two revisions claiming the same parent — is an error rather than a
    guess, because silently picking one drops the other's edits.
    """
    by_parent = {}
    for rev in revisions:
        parent = rev.server_revision_id
        if parent in by_parent:
            return [], "fork: two revisions share parent %s" % parent
        by_parent[parent] = rev

    ordered, seen = [], set()
    current = base_revision_id or "START_REVISION"
    while current in by_parent:
        rev = by_parent[current]
        if rev.id in seen:
            return [], "cycle in revision chain at %s" % current
        seen.add(rev.id)
        ordered.append(rev)
        current = rev.next_revision_id
    return ordered, None


def replay_workbook(raw, revisions):
    """
    Rebuild current cell content: {sheet_name: {(col, row): content}}.

    Returns (sheets, errors). A non-empty `errors` means the read is untrusted
    and the caller must not publish.
    """
    errors = []
    if isinstance(raw, str):
        raw = json.loads(raw)
    grids, names = _load_base(raw)

    ordered, chain_error = _order_revisions((raw or {}).get("revisionId"), revisions)
    if chain_error:
        return {}, [chain_error]

    # Undo/redo name the revision they cancel, so resolve them up front.
    undone = set()
    for rev in ordered:
        try:
            payload = json.loads(rev.commands or "{}")
        except ValueError:
            return {}, ["revision %s holds invalid JSON" % rev.id]
        kind = payload.get("type") or rev.type
        if kind == "REVISION_UNDONE":
            undone.add(payload.get("undoneRevisionId"))
        elif kind == "REVISION_REDONE":
            undone.discard(payload.get("redoneRevisionId"))

    for rev in ordered:
        payload = json.loads(rev.commands or "{}")
        kind = payload.get("type") or rev.type

        if kind == "SNAPSHOT":
            # The client folded everything so far into a fresh base.
            grids, names = _load_base(payload.get("data") or {})
            continue
        if kind in ("REVISION_UNDONE", "REVISION_REDONE"):
            continue
        if rev.next_revision_id in undone:
            continue

        for cmd in payload.get("commands") or []:
            ctype = cmd.get("type")
            if ctype in IGNORABLE_COMMANDS:
                continue
            sid = cmd.get("sheetId")

            if ctype == "UPDATE_CELL":
                grid = grids.setdefault(sid, {})
                pos = (int(cmd.get("col", 0)), int(cmd.get("row", 0)))
                content = cmd.get("content")
                # A cleared cell arrives as absent/empty content, not as its own
                # command type.
                if content in (None, ""):
                    grid.pop(pos, None)
                else:
                    grid[pos] = content
            elif ctype in ("CLEAR_CELL", "CLEAR_CELLS", "DELETE_CONTENT"):
                _clear_zones(grids.setdefault(sid, {}), _zones(cmd))
            elif ctype == "ADD_COLUMNS_ROWS":
                base = int(cmd.get("base", 0))
                if cmd.get("position") == "after":
                    base += 1
                _shift(grids.setdefault(sid, {}), cmd.get("dimension"),
                       base, int(cmd.get("quantity", 1)))
            elif ctype == "REMOVE_COLUMNS_ROWS":
                grid = grids.setdefault(sid, {})
                # Highest index first, so the earlier ones stay valid.
                for element in sorted((int(e) for e in cmd.get("elements") or []), reverse=True):
                    _shift(grid, cmd.get("dimension"), element, 1, removing=True)
            elif ctype == "CREATE_SHEET":
                grids.setdefault(sid, {})
                names[sid] = cmd.get("name") or sid
            elif ctype == "RENAME_SHEET":
                names[sid] = cmd.get("name") or names.get(sid, sid)
            elif ctype in ("DELETE_SHEET", "REMOVE_SHEET"):
                grids.pop(sid, None)
                names.pop(sid, None)
            elif ctype == "DUPLICATE_SHEET":
                new_id = cmd.get("sheetIdTo")
                if new_id:
                    grids[new_id] = dict(grids.get(sid, {}))
                    names[new_id] = cmd.get("name") or new_id
            else:
                # Possibly content-affecting (SORT_CELLS, CUT/PASTE, MOVE_RANGES).
                errors.append("unsupported command %s in revision %s" % (ctype, rev.id))

    if errors:
        return {}, errors
    return {names.get(s, s): g for s, g in grids.items()}, []


def _grid_to_rows(grid):
    if not grid:
        return []
    max_col = max(c for c, _ in grid)
    max_row = max(r for _, r in grid)
    return [[str(grid.get((c, r), "")).strip() for c in range(max_col + 1)]
            for r in range(max_row + 1)]


class MintConfig(models.Model):
    _inherit = "mint.config"

    # ------------------------------------------------------------------
    # MINTLE schedule sync
    # ------------------------------------------------------------------

    @api.model
    def _mintle_parse_rows(self, rows):
        """
        Rows -> ({date: entry}, warnings).

        Columns are located by HEADER NAME, not position, so reordering or
        inserting a column in the workbook cannot silently shift the data.
        """
        warnings = []
        header_index = None
        for i, row in enumerate(rows[:20]):
            lowered = [c.strip().lower() for c in row]
            if "date" in lowered and "word" in lowered:
                header_index = i
                break
        if header_index is None:
            return None, ["no header row with 'Date' and 'Word' columns"]

        header = [c.strip().lower() for c in rows[header_index]]

        def col(*aliases):
            for alias in aliases:
                if alias in header:
                    return header.index(alias)
            return None

        i_date = col("date")
        i_word = col("word")
        i_hint = col("hint")
        i_query = col("shop query", "query", "shop_query")
        i_label = col("shop label", "label", "shop_label")

        def cell(row, idx):
            if idx is None or idx >= len(row):
                return ""
            return (row[idx] or "").strip()

        schedule = {}
        for row_number, row in enumerate(rows[header_index + 1:], start=header_index + 2):
            date = cell(row, i_date)
            word = cell(row, i_word).upper()
            if not date and not word:
                continue  # blank spacer row
            if not DATE_RE.match(date):
                warnings.append("row %s: '%s' is not a YYYY-MM-DD date" % (row_number, date))
                continue
            if not WORD_RE.match(word):
                warnings.append("row %s: '%s' is not five letters A-Z" % (row_number, word))
                continue
            if date in schedule:
                warnings.append("row %s: duplicate date %s, keeping the first" % (row_number, date))
                continue

            entry = {"word": word}
            hint = cell(row, i_hint)
            if hint:
                entry["hint"] = hint

            query = cell(row, i_query).lower()
            if query:
                # The storefront's menu search matches ONE substring — it does
                # not tokenise — so a multi-word query matches nothing at all.
                # Verified live: "enjoymint" returns products, "enjoymint vapes"
                # returns zero. Drop the query rather than ship a dead link;
                # the label is where extra words belong.
                if re.search(r"\s", query):
                    warnings.append(
                        "row %s: shop query '%s' has a space; a multi-word query "
                        "matches nothing upstream, so it was dropped" % (row_number, query)
                    )
                else:
                    entry["shop"] = {"q": query, "label": cell(row, i_label) or query}

            schedule[date] = entry

        return schedule, warnings

    @api.model
    def _mintle_read_schedule_sheet(self):
        """Replay the workbook and return (rows, errors)."""
        if "spreadsheet.spreadsheet" not in self.env:
            return None, ["spreadsheet_oca is not installed"]

        sheet = self.env["spreadsheet.spreadsheet"].sudo().search(
            [("name", "=", SHEET_NAME)], limit=1, order="id desc")
        if not sheet:
            return None, ["no spreadsheet named %r" % SHEET_NAME]

        revisions = self.env["spreadsheet.oca.revision"].sudo().search(
            [("model", "=", "spreadsheet.spreadsheet"), ("res_id", "=", sheet.id)],
            order="id asc")

        sheets, errors = replay_workbook(sheet.spreadsheet_raw, revisions)
        if errors:
            return None, errors
        if TAB_NAME not in sheets:
            return None, ["workbook has no %r tab (found: %s)"
                          % (TAB_NAME, ", ".join(sorted(sheets)) or "none")]
        return _grid_to_rows(sheets[TAB_NAME]), []

    @api.model
    def cron_sync_mintle_schedule(self):
        """Publish the workbook into the `mintle_schedule` config key."""
        rows, errors = self._mintle_read_schedule_sheet()
        if errors:
            # Leave the previous value in place — a stale-but-good schedule
            # beats a wrong one.
            _logger.error("MINTLE schedule sync aborted: %s", "; ".join(errors))
            return False

        schedule, warnings = self._mintle_parse_rows(rows)
        if schedule is None:
            _logger.error("MINTLE schedule sync aborted: %s", "; ".join(warnings))
            return False
        for warning in warnings:
            _logger.warning("MINTLE schedule: %s", warning)
        if not schedule:
            _logger.error("MINTLE schedule sync aborted: sheet produced no valid rows")
            return False

        payload = json.dumps(schedule, sort_keys=True)
        record = self.sudo().search([("key", "=", CONFIG_KEY)], limit=1)
        description = (
            "MINTLE daily word schedule. Source of truth is the %r spreadsheet; "
            "written by cron_sync_mintle_schedule. Do not edit by hand — the next "
            "sync overwrites it." % SHEET_NAME
        )
        if record:
            if record.value != payload:
                record.write({"value": payload, "description": description, "is_active": True})
                _logger.info("MINTLE schedule updated: %s dates", len(schedule))
            else:
                _logger.info("MINTLE schedule unchanged: %s dates", len(schedule))
        else:
            self.sudo().create({
                "key": CONFIG_KEY,
                "value": payload,
                "description": description,
                "is_active": True,
            })
            _logger.info("MINTLE schedule created: %s dates", len(schedule))
        return True
