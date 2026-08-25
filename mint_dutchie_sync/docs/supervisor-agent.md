# Dutchie Roster Backfill — Supervisor Agent (step 3)

A **functional** daisy.plus agent that watches the customer-roster backfill and
posts a progress digest to a Discuss channel on a schedule. It is a **monitor**,
not the worker: the deterministic `ir.cron` (`_cron_sync_roster`) does the ETL;
the agent only *reads* `mint.dutchie.sync.checkpoint` and reports. This keeps it
within the Agentflow 10-tool-call limit and costs ~one cheap LLM turn per fire
(see [[daisy-plus-rate-limit-masks-zero-balance]] — a runaway agent loop drains
the workspace credit balance, which the cron does not).

## Why an agent and not just another cron

A plain Odoo cron could post a digest, but the agent gives a natural-language
summary, can be DM'd ("how's the backfill?"), and reuses the existing
daisy.plus scheduled-task + Odoo MCP plumbing. The hard ETL stays deterministic.

## System prompt (set on the agent's chatflow)

The scheduled task fires a FIXED input `"Scheduled task execution"`, so all
behavior lives in the system prompt (branch on that trigger string).

```
You are the Dutchie Roster Backfill Supervisor. You MONITOR a customer-data
backfill; you never run or modify it.

When you receive the input "Scheduled task execution", do EXACTLY this, once:

1. Call the Odoo MCP tool `search_records` ONCE:
   model = "mint.dutchie.sync.checkpoint"
   fields = ["name","loc_id","state","rows_done","rows_total","last_run","last_error"]
   domain = []   (all rows; there are ~41)
   Do NOT make per-record calls — one search only (hard 10-tool-call limit).

2. From the result compute:
   - counts by state: done / running / pending / error
   - total rows_done across all stores
   - the list of stores in state "error" with their loc_id + last_error (truncate
     each last_error to ~160 chars)
   - overall progress = done / total stores

3. Call the Odoo MCP tool `send_discuss_message` ONCE:
   channel_id = <MONITORING_CHANNEL_ID>
   body = a concise digest, e.g.:
     "🗂️ Dutchie roster backfill — 28/41 stores done, 1 running, 11 pending,
      1 error. Rows imported: 612,340.
      ⚠️ Errors: loc 1568 — 'roster stream truncated: 360000 of 365526'."
   If there are zero errors, say "no errors" and keep it short.

Rules:
- Read-only. Never call create/write/unlink or any Dutchie tool.
- Exactly one search_records and one send_discuss_message per fire.
- If search_records returns nothing, post "no checkpoints found — backfill not
  provisioned yet" and stop.
- Report numbers verbatim from the tool result; never invent counts or a date.
```

> Replace `<MONITORING_CHANNEL_ID>` with the real Discuss channel id (create a
> channel, e.g. "Dutchie Backfill", and read its `discuss.channel` id).

## Provisioning (LIVE prod action — run manually)

Use `docs/provision_supervisor.py` (next to this file). It:
1. Creates the `daisy.agent` (draft) and runs `action_hire` — which mints the
   bot `res.users`, its own scoped Odoo MCP key, clones the template chatflow,
   and links a Daisy+ key (functional path, see
   [[daisy-functional-agent-provisioning]]).
2. PUTs the system prompt above into the agent's chatflow `agentMessages`, and
   ensures the chatflow's Odoo `customMCP` `mcpActions` allowlist includes
   `search_records` and `send_discuss_message` (a stale allowlist silently
   disables the tools — see [[daisy-plus-provisioning]]).
3. Creates the daisy.plus scheduled task (6-field quartz, UTC), e.g. every 2h:
   `POST /api/v1/scheduled-tasks {name, cronExpression:"0 0 */2 * * *", agentflowId}`.

The agent's backing user is `base.group_user`, which has read on
`mint.dutchie.sync.checkpoint` (granted in `ir.model.access.csv`) — no extra
rights needed for a read-only monitor.

## Activation checklist (tie-in with steps 1 & 2)

Do these IN ORDER; the agent has nothing to report until the backfill runs:

1. Deploy the module: manual `-u mint_dutchie_sync` (modules do NOT auto-deploy).
2. Set the PII key: `DUTCHIE_PII_FERNET_KEY` env var on the Odoo service.
3. Set config params: `mint.dutchie_sync.invsvc_url` (or rely on the default)
   and `mint.inventory_service.api_key`.
4. Deploy the mintinvsvc endpoint to BOTH Railway services and confirm
   `/dutchie/customer-roster` responds (full API key required).
5. Seed `mint.dutchie.sync.checkpoint` rows: one per store with `loc_id` +
   `lsp_id` (+ `company_id`). Use the store→LocId/LspId map.
6. Enable the cron `ir_cron_dutchie_roster_sync` (ships INACTIVE). Run the
   initial backfill OFF-HOURS (a big store holds an open txn for minutes).
7. Provision the supervisor agent (`provision_supervisor.py`) and verify one
   manual fire posts a digest.
```
