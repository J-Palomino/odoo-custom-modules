# 19.0.4.0.0 Deployment Handoff

This release introduces the National Promo Log model and splits
`mint.discount.is_active` into `is_published` + computed `is_active`
(driven by valid window + day-of-week + start/end time-of-day).

## Order of operations

### 1. Verify mintinvsvc tolerates new webhook keys (Phase 0)

The Odoo → mintinvsvc webhook payload now includes:
- `is_published` (bool)
- `is_available_online` (bool)
- `start_time` (float, hour-of-day, 0-24)
- `end_time` (float)

**Current state:** the receiver at
`packages/inventory-service/api/server.js:1415` (`POST /api/webhook/ptl-discount-sync` → `persistPtlDiscounts` at L965)
is permissive — Express JSON parsing ignores unknown fields and the
INSERT lists explicit columns, so extra keys won't crash anything.

**Follow-up (separate PR, not blocking):** to actually consume
`is_published`/`start_time`/`end_time` downstream, add those columns
to the `discounts` table and include them in the INSERT/ON CONFLICT
clause. Without this, `is_active` is the only field POS/storefront sees
— so the Odoo-side `_compute_is_active` (which folds time-of-day in)
remains the single source of truth.

### 2. Bump module version + run migration

```
cd /Users/Keymaker/code/odoo-custom-modules
# Verify clean working tree first
git status -s mint_command_center mint_api_v2 mint_redis_push
```

In Odoo: **Apps → Mint Command Center → Upgrade**. The pre-migrate
(`migrations/19.0.4.0.0/pre-migrate.py`) seeds `is_published`
from current `is_active` and adds 24/7 dow flags to legacy
dutchie/manual records. The post-migrate eagerly recomputes
`is_active` for all 2,597 records.

### 3. Verify

```
# 1. is_published seeded correctly
SELECT source, COUNT(*) FILTER (WHERE is_published) AS published,
                       COUNT(*) AS total
FROM mint_discount GROUP BY source;
# Expect: dutchie ~1,104 published, manual ~329, ptl 0 (PTL re-publishes
# from the daily lifecycle cron).

# 2. is_active recomputed
SELECT source, COUNT(*) FILTER (WHERE is_active) AS active,
                       COUNT(*) AS total
FROM mint_discount GROUP BY source;

# 3. Hourly cron registered
SELECT name, interval_number, interval_type, active
FROM ir_cron WHERE name LIKE 'PTL%';
```

### 4. Backfill the National Promo Log

```
cd /Users/Keymaker/code/odoo-custom-modules/mint_command_center/scripts
export ODOO_KEY=<your api key>
python3 backfill_2026_national_promo_log.py --dry-run --state AZ 2>unmatched.tsv
# Review unmatched.tsv for brands that didn't resolve.
# Add missing mint.brand records, then re-run without --dry-run:
python3 backfill_2026_national_promo_log.py 2>unmatched.tsv

python3 backfill_brands_outreach_status.py --dry-run
python3 backfill_brands_outreach_status.py
```

### 5. End-to-end smoke test

1. CRM: create `crm.lead`, set `vendor_brand_id = Aeriz`, click
   **Create Deal Submission**. Confirm new `mint.deal.submission`
   pre-filled with brand + lead.
2. Submission: fill funding amount, **Approve**. Confirm a
   `mint.national.promo(Aeriz, AZ, 2026)` is created/linked.
3. Convert to PTL deal: **Convert to Deal**. Confirm
   `mint.ptl.deal.vendor_funding_amount` populated.
4. Calendar entry: open from the campaign form's Entries tab.
   Set `is_published=True`. Confirm cascade through
   `mint.ptl.day.action_publish` → `mint.discount(is_published=True)`.
5. Discount: set `start_time=14.0`, `end_time=16.0`, today's dow=True.
   Trigger `_cron_recompute_active` — verify `is_active` flips at
   14:00 and back at 16:00.
6. `curl /api/v1/discounts/daily-deals` — confirm carousel returns
   published deals within next 7 days.

### Rollback

If the compute causes problems:

```sql
-- 1. Drop is_active stored values; restore as plain Boolean
ALTER TABLE mint_discount ALTER COLUMN is_active DROP DEFAULT;
UPDATE mint_discount SET is_active = is_published;

-- 2. In code: remove the override in mint_discount_ext.py
-- 3. Revert __manifest__.py version to 19.0.3.3.0
-- 4. Restart Odoo
```

The `is_published` column stays — it's harmless if unused.
