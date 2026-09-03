# Mint PostHog — Odoo error and experience logging

Ships Odoo failures to the **LetsGoMint** PostHog project (544449), from both
the browser and the server, keyed so a user's client and server errors land on
the same person.

## Why more than one hook

There is no single place in Odoo where all errors pass. Five capture points
are needed because each is blind to what the others see:

| Capture point | Where | Catches | Blind to |
|---|---|---|---|
| `posthog_boot.js` | web client | JS crashes, RPC failures with the server traceback, slow RPC, session expiry, navigation | anything without a browser |
| `scss_error_capture.js` | web client | a failed SCSS/asset build — Odoo shows it as a sticky notification and a `console.log`, **raising nothing**, so no other hook sees it | anything without a browser |
| `ir.http._handle_error` | request | every exception raised serving a request, **including ones Odoo never logs as errors** (`UserError` → 422, session expiry → redirect) | anything outside a request |
| `ir.cron._callback` | scheduler | cron failures, overruns, and a heartbeat per run | non-cron work |
| root log handler | process | anything logged at ERROR — boot, mail queue, webhooks, workers — plus allowlisted below-ERROR loggers | anything never logged |

`scss_error_capture.js` reports through `posthog.captureException`, not a
hand-built `capture("$exception", …)`. Only the `$exception_list` payload
`captureException` produces earns an `$exception_issue_id`, and the PostHog →
Odoo ticket cron discards any exception without one.

`ir.http` and the log handler overlap on a genuine 500. The exception is
flagged once reported, so it is not counted twice; if the flag cannot be set
the duplicate is allowed, which is the safe direction.

## Events

| Event | Meaning |
|---|---|
| `odoo_rpc_error` | server RPC failure, with Python traceback, model and method |
| `odoo_rpc_slow` | RPC over 10s |
| `odoo_session_expired` | logged out mid-task — emitted by **both** client and server |
| `odoo_request_error` | exception while serving a request, with path, method, status, uid |
| `odoo_request_slow` | request over `MINT_POSTHOG_SLOW_REQUEST_MS` (default 5000) |
| `odoo_cron_failed` | scheduled action raised |
| `odoo_cron_slow` | scheduled action over `MINT_POSTHOG_SLOW_CRON_SECONDS` (default 300) |
| `odoo_cron_run` | heartbeat, max one per cron per hour — **alert on its absence** |
| `odoo_server_error` | ERROR/CRITICAL log record with traceback |
| `odoo_server_log` | allowlisted below-ERROR record (failed logins) |
| `odoo_style_compilation_failed` | SCSS/asset build failed — carries the compiler output and the bundle URL |
| `$exception`, `$pageview` | client-side crashes and navigation |

`odoo_cron_run` exists for a specific failure mode: a cron that stops being
scheduled produces no traceback, so nothing surfaces it. Alert on a missing
heartbeat, not on an error that will never arrive.

## Configuration

All server-side capture is **off unless `MINT_POSTHOG_SERVER_CAPTURE=1`**.
Configuration is read from the environment, never the database, so the logging
path never needs a cursor — it has to work when the transaction is already
aborted.

| Variable | Default | Purpose |
|---|---|---|
| `MINT_POSTHOG_SERVER_CAPTURE` | *(off)* | master switch for all server capture |
| `MINT_POSTHOG_KEY` | LetsGoMint project key | PostHog project |
| `MINT_POSTHOG_HOST` | `https://us.i.posthog.com` | PostHog host |
| `MINT_POSTHOG_SLOW_REQUEST_MS` | `5000` | slow-request threshold |
| `MINT_POSTHOG_SLOW_CRON_SECONDS` | `300` | slow-cron threshold |
| `MINT_POSTHOG_EXTRA_LOGGERS` | `odoo.addons.base.models.res_users:INFO` | below-ERROR allowlist, `logger:LEVEL` comma-separated; set to empty to disable |

The default allowlist exists because Odoo logs failed logins at INFO
(`"Login failed for login:%s from %s"`). Capturing that logger is what makes
"I can't log in" visible without overriding the authentication path itself.

## What is deliberately NOT captured

- **`request.params`** — routinely carries passwords, API keys and customer
  PII. The path is recorded; the payload never is. Asserted by the checks.
- **`odoo.sql_db`** — logs failing SQL verbatim, which can carry customer
  data. The exception still reaches us via the ORM caller.
- **`werkzeug`** — logs every request at INFO; real failures arrive through
  `ir.http._handle_error` with better context.
- **404s and redirects** — routing misses, mostly bots.
- **Exceptions swallowed by `try/except: pass`** in custom modules. No hook
  can see these. Finding them is a code-review job, not an instrumentation
  one.

## Safety

This runs in-process on an ERP serving 43 stores, so:

- `emit()` only builds a dict and does a non-blocking queue put; all network
  I/O is on one daemon thread.
- The queue is bounded (1000). Overflow is dropped, counted, and the count
  rides along on the next delivered event — a flood shows up as data, not as
  backpressure.
- Muted loggers plus a thread-local re-entry guard: failing while reporting a
  failure cannot recurse.
- Every core override uses `*args, **kwargs` passthrough rather than
  restating Odoo's signature, and always returns `super()`'s result. Every
  cron and every request goes through these — an upstream signature change
  must not be able to break them.
- Every telemetry path is wrapped. Errors still raise, still propagate, and
  are still served exactly as before.

## Checks

```bash
python3 mint_posthog/dev/offline_checks.py   # 21 checks, no Odoo, no database
```

Odoo is stubbed, so these cover the parts that are ours: the safety properties
above and the contracts with core.
