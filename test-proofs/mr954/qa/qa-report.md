# QA Report: MR-954 — agent reply sanitize + superseded guard + newest-N history

**Date:** 2026-07-13
**Mode:** Backend
**Branch:** `feature/task-954-agent-reply-sanitize` (head 913e1a8; code under test df09b7c)
**Ticket:** https://letsgomint.us/odoo/action-591/954

## Test Results

| Case | Spec | Status | Evidence |
|---|---|---|---|
| U-1 | Odoo unit suite (13 tests) in dockerized pinned `odoo:19` (digest 3eede45a), fresh DB, `-i daisydo_agents --test-tags /daisydo_agents` | ✅ 13/13 PASS, exit 0 | `unit-suite-run2-pass.log` |
| U-1a | First run had 1 test-harness error (mock on compiled `re.Pattern` — read-only attr); fixed in `913e1a8` (test-only change) | ✅ fixed | `unit-suite-run2-pass.log` |
| D-1 | Staging deploy of exact SHA `df09b7c` via serviceInstanceDeployV2 (svc ce39e6ac, env cd502c8e-434b) | ✅ SUCCESS (deployment 4fd58522) | Railway deployment meta |
| D-2 | Staging boot: `/web/login` 200, 0 tracebacks in 401 boot log lines, no daisydo errors | ✅ PASS | boot-log scan |
| D-3 | Staging drift note: `-u` removed `daisy_autorespond_when_away` field/view (staging-branch-only feature not on main) — expected, returns when `staging` branch redeploys | ⚠️ noted | boot log |

## Acceptance Criteria Verification

| AC | How verified | Status |
|---|---|---|
| AC01 bad route stripped + note | unit `test_bad_route_stripped`, `test_nonexistent_action_stripped` | ✅ unit |
| AC02 valid links byte-identical (incl. trailing punctuation) | unit `test_valid_link_untouched`, `test_valid_link_with_trailing_punctuation` | ✅ unit |
| AC03 markdown → "label — url" | unit ×2 | ✅ unit |
| AC04 nonexistent record stripped (Tier 2) + kill switch | unit ×2 | ✅ unit |
| AC05 superseded guard (newer job / other channel / errored) | unit `test_is_superseded` | ✅ unit (live re-check post-deploy) |
| AC06 newest-N chronological history | unit `test_recent_history_newest_n` (25 msgs → newest 20, chronological) | ✅ unit |
| AC07 live figures == spreadsheet_raw ground truth | requires prod Synthia — **deferred to post-deploy battery** (Phase 4 of the Implementation Plan; ground-truth recipe in Research tab) | ⏳ post-deploy |
| AC08 sanitizer fail-open | unit `test_fail_open_on_internal_error` (forced RuntimeError → original text posted, exception logged) | ✅ unit |

## Notes
- Live DM battery (AC01/02/05 end-to-end + AC07) cannot run pre-deploy: Synthia exists only in prod, and staging's March-clone credentials do not match current service env; the battery is the FIRST post-deploy verification step in /pr-deploy.
- Cosmetic: sanitizer log line prints `Agent job []` when invoked on an empty recordset (unit-test context only; real dispatch always has a record).
- Cleanup: qa954 docker containers/network removed; no prod/staging data mutated (staging got a code deploy only).

## Verdict
**QA-cleared for PR + prod deploy**, with the live battery as the mandatory post-deploy gate before MR-954 closes.
