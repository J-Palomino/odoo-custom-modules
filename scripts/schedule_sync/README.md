# Store schedule → Odoo spreadsheet sync

Mirrors the eight Mint store schedule Google Sheets into Odoo as
`spreadsheet.spreadsheet` records — one record per store, one tab per synced
week.

This is **phase 0**. It copies grids verbatim. It does not interpret shifts and
does not touch `hr.employee`, for two verified reasons:

- Odoo Enterprise **Planning is not available** on this instance. The `planning`
  module is `uninstallable` and `planning.slot` is absent from `ir.model`, so
  there is no native model that means "person X works 1:30–9:30 PM on Aug 12".
  `resource.calendar.attendance` only carries `dayofweek`/`hour_from`/`hour_to`/
  `week_type` — a recurring week, not per-date shifts.
- Most people on these sheets **have no employee record**. Of 141 names sampled
  across six sheets, 40 resolved uniquely, 8 were ambiguous and 93 had no match;
  20 of those misses were re-checked against all 220 active *and* archived
  employees and none appeared.

So the useful first step is visibility: get the schedules where people can see
them in Odoo, and let the roster gap be fixed separately.

## Usage

```bash
python3 sync.py                    # all stores, week window around today
python3 sync.py --store Mesa       # one store
python3 sync.py --dry-run          # parse and report, write nothing
python3 sync.py --all-weeks        # every tab — large, Tempe has 176
python3 sync.py --from-xlsx DIR    # use already-downloaded <store>.xlsx
```

Requires `google-auth`, `google-api-python-client`, `requests`, `openpyxl`.

### Bridge path

`from_csv.py` pushes a single week from a base64 Google Sheets CSV export
through the same builder, for when Drive API access isn't wired up:

```bash
python3 from_csv.py --store Mesa --b64 mesa.b64
```

`verify.py` reads a record back out of Odoo and prints the stored grid.

## Google access

`sync.py` resolves a credential in this order:

1. `GOOGLE_SERVICE_ACCOUNT_JSON` — path to a key file
2. `~/gbp-metrics-key.json`
3. `gcloud auth print-access-token`

**Current state:** the service account
`gbp-metrics@letsgomint-us.iam.gserviceaccount.com` authenticates fine and the
APIs are enabled, but Drive returns 404 and Sheets 403 for all eight files —
it simply has no access. Two ways to fix it:

- **Unattended (preferred):** share the eight sheets with the service account
  address as Viewer. Nothing else changes.
- **Interactive:** `gcloud auth login --enable-gdrive-access`, which grants the
  Drive scope the export endpoint needs. Note user credentials expire and need
  periodic re-auth, so this suits manual runs rather than a cron.

Domain-wide delegation is *not* a route here: the SA's DWD grant covers only
`business.manage`, and requesting Drive scopes returns `unauthorized_client`.

## Design notes

- Reads workbooks as **XLSX via the Drive export endpoint**, not the Sheets API
  — export needs only a Drive scope, and XLSX preserves the merged ranges these
  layouts depend on.
- Week identity comes from the **dates embedded in the first rows**, never the
  tab title. Titles are inconsistent, and Tempe and 75th Ave both contain
  duplicate and missing week tabs.
- Cells are written as **plain strings**, matching what live records store.
- The payload goes into **`spreadsheet_binary_data`** as base64 JSON, matching
  the existing GL importer. Writing `spreadsheet_raw` directly is not the
  supported path, though it is readable afterwards.
- Upsert is **by record name**, so re-runs update in place.

## Known limits

- Sheets vary across four to five layouts with different column strides and
  name-column positions. Phase 0 mirrors them as-is, so this does not matter
  yet; it will matter as soon as shifts get parsed.
- `--all-weeks` on Tempe would build a very large document. The default window
  is deliberate.
- Cave Creek has no `res.company` record, so its spreadsheet syncs without a
  company set.
