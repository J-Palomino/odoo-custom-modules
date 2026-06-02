# Mint Industry Trends

Apify-fed industry-trend tracking for cannabis market intelligence. Lives next
to the competitor intel in **Mint Marketing → Industry Trends**.

## What it does

Tracks four industry signals and stores them as a queryable time series in Odoo:

| Signal | `signal_type` | Lands in |
|---|---|---|
| Google/search trends | `search_trend` | `mint.trend.observation` |
| Social / Reddit | `social_mention` | `mint.trend.observation` |
| News | `news` | `mint.trend.observation` |
| Brand / product launches | `product_launch` | `mint.trend.observation` |
| Competitor menus / pricing | `competitor_price` | `mint.competitor.deal` (reused) |

## Models

- **`mint.trend.source`** — one configured Apify actor run. Admin-editable in
  the backend; `last_run_at` / `last_status` / `last_item_count` report feed
  health. The collector reads the active sources here.
- **`mint.trend.term`** — watchlist of brands / strains / categories / topics /
  competitors. `keywords` (comma-separated) is what the collector matches
  scraped items against. `trend_score` is a stored rolling indicator for
  "what's hot" ordering, refreshed daily by the housekeeping cron.
- **`mint.trend.observation`** — time-series datapoints. Deduped on
  `(source_id, external_id)` via a SQL unique constraint; use the
  `upsert(vals)` model method from the collector for idempotent writes.

## Architecture

The **scraping does not run inside Odoo** (long Apify runs would block a cron
worker). It runs in an external k8s CronJob that calls the Apify API and writes
back via XML-RPC — same pattern as `k8s/monitoring/velocity-guard` and the
competitor-analysis cron.

```
k8s CronJob (Node, daily)
  ├─ read active mint.trend.source records (XML-RPC search_read)
  ├─ for each source: run its Apify actor (POST run-sync-get-dataset-items)
  ├─ match items to mint.trend.term by keywords
  ├─ mint.trend.observation.upsert(...)  (idempotent)
  └─ write back source.last_run_at / last_status / last_item_count
```

Collector lives at `k8s/monitoring/industry-trends/` (Phase 4).

## Setup / deploy

1. **Apify token** — create an account at apify.com, copy the API token from
   Settings → Integrations, store it as k8s secret `apify-credentials/api-token`.
2. **Confirm actor slugs** — the seed `mint.trend.source` records use best-known
   store actors with `CONFIRM…` notes. Validate each actor's input/output
   schema against the live Apify account, then clear the note.
3. **Install** — module is in the Dockerfile COPY list. Deploy then
   `-u mint_industry_trends` (custom modules do NOT auto-upgrade on push).
4. **Populate** the watchlist (`mint.trend.term`) with the brands/strains/
   categories to track and link them to sources.
5. **Enable** the k8s CronJob.

## Status

- ✅ Phase 1: Odoo data model + UI + housekeeping cron (this module)
- ⛳ Phase 2: Apify account + token
- ⛳ Phase 3: collector script (verified against live Apify output)
- ⛳ Phase 4: k8s CronJob + secret + deploy
