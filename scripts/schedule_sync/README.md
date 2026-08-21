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
python3 sync.py --all-weeks        # every tab, into a separate archive record
python3 sync.py --from-xlsx DIR    # use already-downloaded <store>.xlsx
python3 audit.py                   # coverage + fidelity vs the source sheets
python3 roster_report.py           # who is named on the imported schedules
```

Two record families, deliberately separate:

| Record | Written by | Contents |
|---|---|---|
| `Schedule — <store>` | `sync.py` | rolling window around today |
| `Schedule — <store> (all weeks)` | `sync.py --all-weeks` | every tab in the workbook |

They must not share a name: the rolling sync upserts by name and holds far
fewer weeks, so a routine run would otherwise overwrite the archive.

Full history as imported 2026-08-20: **695 tabs, 394,350 cells** across the
eight stores (Tempe alone is 225 tabs / 194,197 cells).

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

1. the OAuth refresh token stored in Odoo — **preferred**
2. `GOOGLE_SERVICE_ACCOUNT_JSON` — path to a key file
3. `~/gbp-metrics-key.json`
4. `gcloud auth print-access-token`

### Setting up the OAuth path (one time)

Reuses the Google OAuth client the Odoo/Daisy stack already owns
(`google_calendar_client_id` / `_secret` in `ir.config_parameter`, project
`letsgomint-us`) — no new credential is introduced. Consent once, and the
refresh token is stored as `schedule_sync.google_refresh_token` for unattended
runs thereafter.

```bash
python3 oauth_setup.py            # loopback listener on :8765
python3 oauth_setup.py --manual   # paste the code instead
python3 oauth_setup.py --check    # show state, prove the token still refreshes
```

Approve with the account that can see the sheets. `wizard@brightroot.com`
resolves to the same Google identity as `jpalomino@brightroot.com`, which is
why that account can read all eight.

The default flow needs `http://localhost:8765/` registered on the OAuth client.
Whether it is registered could not be determined from outside — Google defers
`redirect_uri` validation until after sign-in, so probing the consent endpoint
proves nothing. If you get `redirect_uri_mismatch`, either add that URI in the
Cloud console or use `--manual`, which goes through
`https://letsgomint.us/google_account/authentication` (already registered for
the calendar integration) and has you copy the `code` out of the address bar.

### Why not the service account

`gbp-metrics@letsgomint-us.iam.gserviceaccount.com` authenticates fine and the
APIs are enabled, but Drive returns 404 and Sheets 403 for all eight files — it
has no access. Sharing the sheets with that address would also work. Domain-wide
delegation is *not* a route: the SA's DWD grant covers only `business.manage`,
and requesting Drive scopes returns `unauthorized_client`.

## Design notes

- Reads workbooks as **XLSX via the Drive export endpoint** — export needs only
  a Drive scope, and XLSX preserves the merged ranges these layouts depend on.
- Falls back to the **Sheets API** when Drive refuses to export. Tempe carries
  176 weeks and trips `exportSizeLimitExceeded`; the API path fetches only the
  selected weeks, so it is both a workaround and cheaper.
- **Credentials are probed, not assumed.** Sharing differs per sheet — the
  service account can read Tempe but not Mesa — so every candidate credential
  must read *all* target sheets before it is used. Picking one that works for
  the first sheet leaves a run failing halfway through.
- Week identity comes from the **dates embedded in the first rows**, never the
  tab title. Titles are inconsistent, and Tempe and 75th Ave both contain
  duplicate and missing week tabs.
- Cells are written as **plain strings**, matching what live records store.
- The payload goes into **`spreadsheet_binary_data`** as base64 JSON, matching
  the existing GL importer. Writing `spreadsheet_raw` directly is not the
  supported path, though it is readable afterwards.
- Upsert is **by record name**, so re-runs update in place.

## Scheduling

**Nothing runs this automatically.** There is no `ir.cron`, no crontab entry
and no launchd agent — every sync so far has been invoked by hand. Deciding
where it runs is still open, and it turns on the credential:

- The current credential is a **gcloud user token for
  jpalomino@brightroot.com, which expires** and needs an interactive re-login.
  That is fine for manual runs and unsuitable for a cron.
- For anything unattended, share the eight sheets with the service account
  first. Then the job has a credential that does not expire.

Options, roughly in order of durability:

| Where | Needs | Notes |
|---|---|---|
| Odoo `ir.cron` | code moved into an Odoo module + SA credential | Runs where the data lands; 54 crons already run there |
| Railway / Coolify worker | SA credential | Independent of anyone's laptop |
| launchd on this Mac | SA credential, machine awake | Simplest, least reliable |

## Styling

Cell styling **is** imported: fill colour, bold, italic and font colour, using
the keys o-spreadsheet actually stores (`fillColor`, `textColor`, `bold`,
verified against live records). Styles are deduplicated into a shared table —
the same handful of fills repeats thousands of times, so 58,099 styled cells in
Cave Creek collapse to 95 distinct styles.

This matters because these schedules encode meaning in colour, not just text:
Cave Creek's tabs carry four heavily-used fills, and a plain-text import
silently drops whatever they signify.

The Sheets API path fetches formatting via `includeGridData`, which is much
heavier than values, so it is skipped when more than `style_tab_limit` (25)
tabs are selected. In practice that means **Tempe's full archive has no
styling** while its rolling window does.

## Verified coverage

Audited 2026-08-20 with `audit.py`, comparing source sheets against what Odoo
actually stores:

- **Fidelity: exact.** Every non-empty source cell in a synced tab is present
  in Odoo — zero delta on all eight stores, and the archive records match the
  importer's counts cell for cell.
- **Formulas are safe.** 7,822 formula cells sampled across three workbooks;
  none lacked a cached value, so `data_only=True` loses nothing.
- **Ranges are exact.** The API path derives each tab's range from its real
  `gridProperties` rather than a fixed cap, which would silently truncate
  larger tabs.
- **Undated tabs are excluded from the rolling window** — they carry no
  parseable date row so no week can be assigned. Mostly empty `SheetNN`
  placeholders, but six hold real content (East Mesa `MASTER`, `Lead Base
  Schedule`, `TEAM Shirt Sizes`; Northern `Time Off Requests`; El Mirage
  `HEATHERS COPY`). All of them **are** captured by `--all-weeks`.

## Known limits

- Sheets vary across four to five layouts with different column strides and
  name-column positions. Phase 0 mirrors them as-is, so this does not matter
  yet; it will matter as soon as shifts get parsed.
- `--all-weeks` on Tempe would build a very large document. The default window
  is deliberate.
- Cave Creek has no `res.company` record, so its spreadsheet syncs without a
  company set.
