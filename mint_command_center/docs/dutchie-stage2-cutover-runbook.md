# Dutchie publish — Stage 2 (#2) cutover runbook

Cutting the deal→Dutchie publisher over to the **unified, consolidated** scheme
(branch `feat/dutchie-unify-stage2`, PR #182, mcc `19.0.6.55.0`).

## What changes & the dup-guard model

Two paths publish a deal to Dutchie — submission convert auto-publish (path-1)
and the PTL Publish button (path-2). Stage 2 converges both onto: one
**consolidated** record per (deal, market) (`LocId:0` + `LocationRestrictions`),
a **unified ExternalId** `lgm_deal_<dealId>_lsp<lsp>[_w<k>]`, and a **shared deal
registry** `mint.ptl.deal.dutchie_publish_ids` (`{external_id: dutchie_id}`).

Three guards stop duplicates, in order:
1. **Bilateral mutex** (`mint.ptl.deal._dutchie_claim`) — only the first path to
   publish a deal live ever writes it. **First line; always on.**
2. **Shared registry** — both paths resolve the same record id by ExternalId.
3. **Dutchie read-back** — fallback when the registry is empty.

> ⚠️ The read-back has two known gaps: it (a) **filters expired discounts** and
> (b) matches the **new** ExternalId, which **pre-cutover live records don't
> carry** (they use `lgm_<sub>` / legacy path-2 ids). So for **already-published
> deals the seed migration is the real dup-guard**, not the read-back. Verifying
> migration coverage (Step 4) is the load-bearing step of this cutover.

LSP is read **only** from `res.company.dutchie_lsp_id` (no region-name fallback);
a deal whose stores have no `dutchie_lsp_id` will be **skipped**, not guessed.

## Railway / Odoo IDs

- Prod Odoo svc `ce39e6ac-8784-4580-b462-3da0e571668a`, env `1e6ead7c-b733-43c0-9573-dc206928e773`, project `890ce2d4-e8b1-424d-91db-e0cfd496a4a0`.
- Prod `ODOO_UPDATE_MODULES` (capture before changing, restore after):
  `mint_maintenance_form,mint_banner,mint_embed,mint_flipbook,mint_account,mint_command_center,mint_pos_bridge`

---

## Step 0 — Pre-flight (no changes)

- [ ] PR #182 reviewed/approved.
- [ ] Record current prod `mcc` version (expected `19.0.6.54.0`) and the current
      values of **both** mode params:
      `dutchie.publish.mode`, `mint.dutchie_discount_push.mode`.
- [ ] Confirm every **active** market has stores with a non-zero
      `dutchie_lsp_id` (single source now; no fallback):
  ```python
  # RPC: regions whose stores lack an LSP would be silently un-publishable
  comp = env['res.company'].search([('dutchie_pos_location_id','!=',False)])
  missing = comp.filtered(lambda c: not c.dutchie_lsp_id)
  # missing must be empty (or those stores are intentionally non-Dutchie)
  ```
- [ ] Low-traffic window chosen (there is a brief stacked-discount moment per
      deal during path-2 cutover — see Step 6).

## Step 1 — Merge to main

- [ ] Merge PR #182 → `main`. Prod auto-builds `main` HEAD, **but a plain build
      restart does NOT run `-u`** and this release adds the
      `dutchie_publish_ids` column — see Step 2.

## Step 2 — Deploy WITH a real `-u` (schema change!)

`serviceInstanceDeployV2` does a fast restart that **skips the entrypoint `-u`**,
so the new column/migration won't apply. Force a full entrypoint build via a
**variable-change redeploy**:

```bash
# triggers a build that runs the entrypoint (-u mint_command_center) + the migration
# (re-set ODOO_UPDATE_MODULES to the SAME value to cause a change-event, or toggle)
variableUpsert(project 890ce2d4…, env 1e6ead7c…, svc ce39e6ac…,
               name=ODOO_UPDATE_MODULES, value="<original 7-module list>")
```

- [ ] Wait for the deployment to reach **SUCCESS**.
- [ ] Verify `mcc` installed_version = `19.0.6.55.0` AND the column exists:
  ```python
  env['mint.ptl.deal'].search_count([('dutchie_publish_ids','!=',False)])  # no error = column exists
  ```

## Step 3 — Read the migration log

The `19.0.6.55.0` post-migrate seeds the registry from path-1 submission maps.

- [ ] In the deploy logs, find:
  `Stage 2 migration: seeded dutchie_publish_ids on N deal(s)`
- [ ] **The `SKIPPED (no LSP …)` warning MUST be empty.** If it lists submission
      ids, those deals' markets/stores have no `dutchie_lsp_id` → fix the data
      (set the store LSP) and re-run `-u` before continuing — otherwise they dup.

## Step 4 — Verify registry coverage (LOAD-BEARING)

For every **already-published path-1 deal**, confirm the registry is seeded — this
is what prevents dups for pre-cutover deals (read-back can't find their old-keyed
records).

```python
# every submission that was published (non-empty map) must have its deal seeded
import json
subs = env['mint.deal.submission'].search([('deal_id','!=',False),
                                            ('dutchie_publish_loc_ids','!=',False)])
gaps = []
for s in subs:
    old = json.loads(s.dutchie_publish_loc_ids or '{}')
    has_consolidated = any(isinstance(v,int) and not isinstance(v,bool) and v>0 for v in old.values())
    if has_consolidated and not (s.deal_id.dutchie_publish_ids or '').strip(' {}'):
        gaps.append(s.id)
# gaps MUST be empty before going live
```

- [ ] `gaps` is empty.
- [ ] (path-2 deals previously published per-store are handled by cutover-adoption
      at first publish — no migration entry expected; that's fine.)

## Step 5 — Dry-run on real data (payload shape)

Temporarily set **both** `dutchie.publish.mode` and
`mint.dutchie_discount_push.mode` to **dry-run** (record originals). Publish a
representative deal via **both** entry points; inspect the logged payloads:

- [ ] Both produce the SAME `lgm_deal_<id>_lsp<lsp>` ExternalId.
- [ ] `LocId:0`-style consolidated record with all target stores in
      `LocationRestrictions`.
- [ ] Correct **per-market** weekday flags (no cross-market contamination).

> Dry-run validates *shape only* — it never reads back or creates, so it can't
> exercise #1/#2. The dup-guard is Steps 3–4 + the mutex.

## Step 6 — Go live (canary first)

- [ ] Align modes to the intended LIVE values (avoid one `off` + one `live` —
      that breaks expire/deactivate symmetry).
- [ ] **Canary:** publish ONE deal that was previously published **per-store via
      path-2**. In the push log, confirm:
  - it **adopted** the owner store's existing id (update, not a fresh create),
  - the non-owner per-store records were **deactivated** (one-time cleanup),
  - exactly **one** discount for that (deal, market) now exists in Dutchie
    Backoffice — no duplicate, correct LocationRestrictions + weekdays.
- [ ] Then publish a path-1 (convert) deal and confirm update-in-place via the
      seeded registry (no new record).

## Step 7 — Roll forward & verify

- [ ] Publish the remaining deals.
- [ ] Spot-check Dutchie Backoffice: one record per (deal, market); **no
      duplicate discounts**; legacy per-store records `IsDeleted`.
- [ ] Scan `mint.dutchie.discount.push.log` for `SKIPPED` rows
      (read-back abort / no-LSP / all-False / resurrection) and resolve.

## Step 8 — Cleanup

- [ ] Restore `ODOO_UPDATE_MODULES` to its original value.
- [ ] Restore any temporarily-changed mode params.
- [ ] Monitor a few publish cycles.

## Rollback

The mutex stopgap (already in `6.53.0`) prevents NEW cross-path dups regardless,
so rollback is low-stakes:
- Revert prod to the pre-cutover commit (`6.54.0`); consolidated records already
  written remain valid (the stopgap still guards).
- Any duplicate observed: deactivate via the existing `IsDeleted` re-POST path
  (`/api/admin/discounts` with `IsDeleted:true`).
- Restore `ODOO_UPDATE_MODULES`.

## Known follow-up (optional, post-cutover)

Close read-back gap #1 by adding a by-ExternalId read that **includes expired**
records, so the read-back becomes a real second guard instead of leaning on the
migration. Not required for cutover if Steps 3–4 pass.
