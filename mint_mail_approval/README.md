# Mint Mail Approval Gate

Holds outgoing email for human approval. Built so SMTP can finally be turned on
without the instance blasting months of auto-generated mail.

## Why this exists

As of 2026-08-07 prod Odoo had **zero `ir.mail_server`** records, so nothing had
sent since 2026-02-17. Two weeks of traffic (2026-07-24 → 08-07) was 223 queued
mails, ~16/day, of which 133 were automatic customer "Welcome to Mint Cannabis!"
messages and 45 were maintenance-ticket follower notifications. None of it had
been seen by a person.

## How it layers with `mint_mail_whitelist`

Two independent gates, different questions, different hook points:

| Module | Question | Hook | Failure mode |
|---|---|---|---|
| `mint_mail_whitelist` | **Who may receive?** | `create()` | Empty allowlist blocks everything |
| `mint_mail_approval` | **Did a human approve?** | `send()` | No matching rule holds the mail |

Neither depends on the other. Removing one does not open the other's hole.

## The choke point

`mail.mail.send()` is overridden. That single method is what both
`process_email_queue` (the queue cron) and every `force_send=True` template send
funnel through, so no send path bypasses review. Held mails stay in state
`outgoing` and are re-evaluated harmlessly on each cron pass, which keeps them
visible in the queue rather than silently cancelled.

## Rules

`Settings > Technical > Email > Mail Approval Rules`. Evaluated by `sequence`
ascending, **first match wins**. Criteria are ANDed; an empty criterion is not
tested. A mail matching no rule falls back to `mint_mail_approval.default_action`
(ships as `hold`).

Seeded auto-approvals are deliberately narrow — only mail a user personally
triggered and is actively waiting on (password reset, security notices, staff
invitations). Everything else is held.

## Config parameters

| Key | Default | Meaning |
|---|---|---|
| `mint_mail_approval.enabled` | `1` | `0` disables the gate entirely |
| `mint_mail_approval.default_action` | `hold` | Fallback for unmatched mail. Any unrecognised value is treated as `hold` |
| `mint_mail_approval.expire_days` | `7` | Pending mail older than this is cancelled by the daily cron. `0` disables expiry |

## Rollout order

This module is only step 4. Do not skip ahead.

1. Re-mute the internal users that drifted back to `notification_type='email'`
   (17 as of 2026-08-07, including 3 on `@example.com` which hard-bounce).
2. Cancel the 79 stale `state='outgoing'` mails, oldest 2026-06-19 — nothing
   from June should send now.
3. Install this module. With no `ir.mail_server` it is inert, so installing is
   safe and lets you watch the queue fill with what *would* have sent.
4. Tune rules against real traffic for a few days.
5. Add `ir.mail_server` on **staging** first; verify a real send end to end.
6. Prod `ir.mail_server`, plus SPF/DKIM covering **brightroot.com** — 169 of 223
   mails send as `support@brightroot.com`, while the alias domain is
   `letsgomint.us`.
7. Only then re-activate cron **#11 "Mail: Email Queue Manager"** (archived since
   2026-02-11). Until it runs, the queue does not drain at all.

Note that step 7 stays a real switch even with this module installed: the 349
`exception` mails are not auto-retried by Odoo, only `outgoing` ones are.

## Tests

```
odoo --test-enable --workers 0 -u mint_mail_approval -d <db>
```

`--workers 0` is mandatory; without it `--test-enable` runs zero tests on this
stack.
