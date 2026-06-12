# Smartsheets MCP — cell/formula editing for agents

Lets the daisy.plus agents (Dwight, Fixit, etc.) read and edit **specific cells and
formulas** in Odoo Smartsheets (`spreadsheet_oca` → `spreadsheet.spreadsheet`), not just
whole-record CRUD.

## Why this is possible / what already existed

- **`spreadsheet_oca` = "Smartsheets"** is installed on prod (was v1.0.2; **257 sheets** —
  sales KPIs, GL Daily Sales, Closing Reports …). The workbook lives in
  **`spreadsheet.spreadsheet.spreadsheet_raw`** — a JSON field holding the full
  o-spreadsheet doc (`version` 18.5.10).
- **Cell encoding (verified on live sheets):** `spreadsheet_raw["sheets"][i]["cells"]` is a
  dict keyed by A1 address whose value is the **plain content string** — a literal, or a
  formula starting with `=`. e.g. `cells["A1"] == "Closing Report — 2026-02-01"`,
  `cells["B5"] == "=SUM(B1:B4)"`.
- **Concurrency-safe write already built:** commit `b1f26a0` added
  **`mcp_safe_write(vals, expected_write_date, expected_revision_id)`** on
  `spreadsheet.abstract` — a `FOR UPDATE` row-locked, revision-aware guarded write made for
  headless edits. It refuses on a stale `write_date` or a moved revision head with a
  `SMARTSHEET_CONFLICT:` error and returns fresh `{write_date, revision_head}` tokens. Plus
  `_spreadsheet_revision_head()` and `get_spreadsheet_data()`.
  - ⚠️ That commit lives on `feat/mint-tech-org` (not main, not deployed). This branch
    (`feat/smartsheets-mcp`) **cherry-picks it off `main`** so the feature ships independently
    of that 141-commit branch.

## Architecture

```
agent (Dwight/Fixit)  --MCP-->  fastapi-mcp (daisydo/fastapi)  --XML-RPC-->  Odoo
                                   smartsheet_* tools                spreadsheet.spreadsheet
                                                                       mcp_read_cells / mcp_set_cells
                                                                         -> mcp_safe_write (guarded)
                                                                            -> spreadsheet_raw
```

Every WRITE routes through `mcp_safe_write`, so the optimistic-concurrency guards are never
bypassed. Reads and writes run as the **agent's own Odoo user** (`check_access`), so an agent
can only touch sheets it is allowed to.

## Phases

### Phase 1 — cell-level model methods ✅ (this branch)
Added to `spreadsheet.abstract`:
- `mcp_read_cells(cells=None, sheet=None)` — read an A1 ref, an `'A1:B3'` range, a list of
  those, or (None) every non-empty cell. Returns `{cells: {A1: content}, write_date,
  revision_head, sheet, sheet_id}`.
- `mcp_set_cells(edits, sheet=None, expected_write_date=None, expected_revision_id=None)` —
  `edits = [{'cell':'A1','content':'=SUM(B1:B5)'}]` (`'formula'` aliases `'content'`; empty
  content clears the cell). Reads `spreadsheet_raw`, patches `sheets[].cells`, calls
  `mcp_safe_write`. Returns `{id, write_date, revision_head, cells_written}`.
- Helpers: `_mcp_find_sheet` (by id/name, default first), `_mcp_expand_range` (A1 / A1:B3),
  `_mcp_cell_content` (tolerates the `{"content": …}` variant), col↔num utils.
- Manifest bumped `…1.0.3 → …1.0.4`.

### Phase 2 — MCP tools (`daisydo/fastapi/app/mcp/tools.py`)
- `smartsheet_list(name?)` — find sheets (or reuse `search_records`).
- `smartsheet_get_cells(sheet_id|name, cells)` → contents + concurrency tokens.
- `smartsheet_set_cells(sheet_id, edits[], expected_write_date?, expected_revision_id?)` →
  calls `mcp_set_cells`; surfaces `SMARTSHEET_CONFLICT` so the agent re-reads & retries.
- Each tool just calls the Phase-1 model methods via `execute_kw`.

### Phase 3 — deploy + wire
- Ship `spreadsheet_oca` ≥ 1.0.4 to prod Odoo (Railway, **manual `-u` upgrade** — modules
  don't auto-deploy; see CLAUDE.md).
- Redeploy `fastapi-mcp` (Railway) with the new tools.
- Add the tool names to the relevant agents' chatflow `mcpActions` (e.g. Dwight, or a
  dedicated spreadsheets agent), then re-provision/PUT the chatflow.

## Caveats (designed-around, must stay true)

- **Formulas are not recomputed headless.** o-spreadsheet evaluates client-side;
  `spreadsheet_raw` stores the formula *content*, not its result. So **setting** a formula
  works; **reading the computed value** returns only what was cached when a human last opened
  the sheet (often absent). True headless evaluation = a server-side o-spreadsheet runner —
  out of scope; build separately only if needed.
- **A headless write unlinks revisions** (the `write()` override) — collaborative history is
  reset. That is exactly why the guards exist: read → edit → write fast, and on
  `SMARTSHEET_CONFLICT` re-read and retry. Don't blind-write while a human is live in the sheet.
- **Pivot/list-driven sheets have empty `cells`** (data generated from `pivots`/`lists`) —
  cell editing applies only to literal-cell sheets, not generated reports.
- **Permissions** flow through the agent's user (`check_access` in both methods +
  `mcp_safe_write`).

## Open decisions
1. Who gets the tools — Dwight only, a dedicated spreadsheets agent, or all template-MCP agents?
2. Do we also need to **read computed formula results** headless? If yes, that's the bigger
   server-side-evaluator piece; otherwise v1 = set/read formula + read literal values.
