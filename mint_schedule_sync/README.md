# Mint Schedule Sync

Pulls the per-store employee schedule Google Sheets into
`spreadsheet.spreadsheet` records on a cron — one record per store, one tab per
synced week, values plus merges plus cell styling.

It does **not** parse shifts and does **not** touch `hr.employee`. This
instance has no Planning module (`planning.slot` is absent), and only about a
third of the people named on these sheets have an employee record at all.

## Why the Sheets API and not a Drive export

Drive's export endpoint refuses workbooks past a size limit — Tempe carries 225
tabs and returns `exportSizeLimitExceeded`. The API path also needs nothing
beyond `requests`, which Odoo already ships: no openpyxl, no google-auth.

## Configuration

All via `ir.config_parameter`:

| Key | Meaning |
|---|---|
| `mint_schedule_sync.sheets` | JSON list of `{store, sheet_id, company}` |
| `mint_schedule_sync.client_id` | Google OAuth client id |
| `mint_schedule_sync.client_secret` | Google OAuth client secret |
| `mint_schedule_sync.refresh_token` | refresh token carrying a Drive scope |
| `mint_schedule_sync.back_days` | window start, default 7 |
| `mint_schedule_sync.forward_days` | window end, default 14 |

`sheets`, `back_days` and `forward_days` are already set on prod. The three
credential keys are not — see below.

## Credential

Prefer a **service account** restricted to these eight files: share each sheet
with the service account address as Viewer, then store its credentials. That
grants access to exactly these sheets and nothing else.

Avoid reusing a personal `gcloud` credential. The one on the maintainer's
machine carries `https://www.googleapis.com/auth/drive` — full read **and
write** access to that person's entire Drive — and `ir.config_parameter` is
readable by any Odoo admin, so storing it here would widen the blast radius
far beyond these schedules.

## Cron

`data/ir_cron.xml` installs "Mint: Sync store schedules from Google Sheets",
every 6 hours, **inactive**. Configure the credential first, then activate it —
otherwise it just logs a failure every six hours.

A single unreachable sheet is logged and skipped; it does not abort the others.

## Gotchas worth keeping

- Week identity comes from the dates **inside** each tab, never the tab title.
  Titles vary per store (`817-823`, `Non PSR 8/17-8/23`,
  `08172026-08232026 (READY)`) and two workbooks have duplicate weeks.
- Tabs sharing a week are **not** de-duplicated. Tempe splits one week across
  `PSR 8/17-8/23` and `Non PSR 8/17-8/23`; collapsing them drops a whole staff
  group with no error.
- The payload goes in `spreadsheet_binary_data` as base64 JSON, not
  `spreadsheet_raw`.
- Fetching styling needs `includeGridData`, much heavier than values, so it is
  skipped past 25 selected tabs.
