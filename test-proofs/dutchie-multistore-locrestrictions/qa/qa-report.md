# QA Report: dutchie-multistore-locrestrictions

**Date:** 2026-06-23
**Source:** PR #181 — regression test for the PR #179 multi-store Dutchie publish consolidation
**Mode:** Backend (Odoo 19 module — `mint_command_center`)
**Branch:** `test/dutchie-multistore-locrestrictions`
**Linked Odoo task:** none (ad-hoc hardening; not a ticketed SDLC item)

## What was QA'd

The change under test is a **regression test** (`mint_command_center/tests/test_dutchie_multistore_publish.py`). For a regression test, "passes on current code" is necessary but not sufficient — QA must also prove the test **fails when the regression is reintroduced**. So this QA runs a baseline (green) plus a mutation (red).

Environment: real Odoo 19 (`odoo:19` at the prod-pinned digest `sha256:3eede45…`) + the repo's exact pip deps, fresh Postgres 15, module installed with `--test-enable --test-tags /mint_command_center:TestDutchieMultistorePublish`.

> Fresh-install shims applied to the **test environment only** (not to the branch): `mint_push` data-file order, a declared `product.template.x_is_cannabis`, and a trim of `mint_command_center`'s `data` list to security+cron. These work around pre-existing fresh-install gaps (see Notes) and do not touch the code under test.

## Test Results

### Backend Mode

| Case | Spec | Status | Evidence |
|---|---|---|---|
| BASE | Both tests pass on current (fixed) code | ✅ Pass — `0 failed, 0 error(s) of 2 tests` | [case-baseline.log](case-baseline.log) |
| MUT | Both tests fail when per-store fan-out is reintroduced | ✅ Guard works — `2 failed, 0 error(s) of 2 tests` | [case-mutation.log](case-mutation.log) |

Mutation applied (simulating the pre-#179 regression): fan out one discount per `(span, loc)` and drop `LocationRestrictions`. Result:

```
FAIL: test_post_count_equals_spans_not_stores
AssertionError: 6 != 2 : POST count must equal the number of spans, not stores
FAIL: test_single_span_one_record_across_all_stores
AssertionError: 3 != 1 : single span must POST exactly one record, not one per store
```

## Acceptance Criteria Verification

- ✅ **Test passes on the fixed publish path** — case BASE (`0 failed of 2`).
- ✅ **Test catches the regression** (one record per store, empty `LocationRestrictions`) — case MUT (`2 failed of 2`, assertion messages name the exact invariant).
- ✅ **Invariant (a): `LocationRestrictions == loc_ids`** — asserted; mutation that empties it turns the test red.
- ✅ **Invariant (b): POST count == spans, not stores** — asserted; mutation that fans out per store turns the test red (6≠2, 3≠1).
- ✅ **Test is registered** (`tests/__init__.py` import) — verified by the test actually executing (2 tests discovered, not 0).

## Notes

- Surfaced pre-existing **fresh-install** gaps in the dependency closure (prod survives them via incremental migration, not a clean install): `mint_push` / `mint_command_center` menu-vs-action data ordering, and ~40 manual/Studio `x_` fields used in code/views but never declared (e.g. `product.template.x_is_cannabis`). Out of scope for this PR; candidate for a separate ticket.
- No state mutated in prod/Dutchie/Odoo — tests stub `_dutchie_build` and mock `urllib.request.urlopen`; nothing leaves the container. No cleanup required.

## Stakeholder review

Baseline passes and the mutation proves the guard. PR #181 is QA-cleared to merge to `main`.
