# Daisy+ Auto-Provisioning — Setup & Usage Guide

When a new **employee** is created in Odoo, this module automatically gives them
a working **Daisy+ AI agent** wired into Odoo. This guide covers how it works,
how to set it up in a fresh environment, how to onboard/use agents day-to-day,
and how to troubleshoot.

> Status: LIVE on production (`letsgomint.us`, `daisydo_agents` ≥ 19.0.5.2.2).
> Tracking task: prod Odoo **project.task #94601** ("Agent Workflow").

---

## 1. What happens on a new hire

When an internal Odoo user that is **linked to an `hr.employee`** is created,
the module automatically:

1. Creates a **`daisy.agent`** record (state = *active*) tied to that employee.
2. **Clones the template chatflow** on Daisy+ (model `gpt-4o-mini` + an Odoo MCP tool).
3. **Mints a Daisy+ API key** and links it to the new chatflow (the agent's "call secret").
4. **Mints a scoped Odoo XML-RPC key** under the employee's **own** user — so the
   agent acts in Odoo with **exactly that employee's permissions**, nothing more.
5. Creates a **`<Name> - Email`** inbox project (invited-only; the employee is
   Project Manager + follower).

All Daisy+ calls are **best-effort**: a failure is recorded on the agent
(`provision_state` / `provision_error`) and **never blocks** user/employee creation.

**Service / integration users are skipped** — only users linked to an
`hr.employee` get an agent. (A `hr.employee` create/`write(user_id)` hook also
catches "user created first, employee linked later" onboarding.)

---

## 2. Components

| Piece | Where |
|---|---|
| Odoo module | `daisydo_agents` (repo `J-Palomino/odoo-custom-modules`, branch `main`) |
| Provisioning code | `daisydo_agents/models/daisy_provisioning.py` |
| Daisy+ template chatflow | `Agent Template (Odoo MCP)` — id `51defb84-c029-4a71-a5c3-d16683336cf7` |
| Odoo MCP server | `https://fastapi-mcp-production.up.railway.app` — SSE at **`/mcp/sse`** |
| MCP auth | headers `X-Odoo-Username` + `X-Odoo-API-Key` (the agent's minted RPC key) |

### Config parameters (`Settings → Technical → System Parameters`)

| Key | Value | Notes |
|---|---|---|
| `daisy.autocreate_agents` | `True` | **Master switch.** `False` pauses everything. |
| `daisy.template_chatflow_id` | `51defb84-…` | Chatflow that gets cloned per agent. **Required.** |
| `daisy.global_api_key` | *(workspace key)* | Daisy+ workspace key used for provisioning. Falls back to `daisy_bot.api_key` if unset. |
| `daisy.default_model` | `gpt-4o-mini` | Model for cloned agents. Optional. |
| `daisy.mcp_server_url` | `https://fastapi-mcp-production.up.railway.app` | Optional; default. |

---

## 3. One-time setup (fresh environment / staging)

> Production is already configured. Use this to stand it up elsewhere.

1. **Deploy** `daisydo_agents` ≥ 19.0.5.2.2 and **upgrade the module**
   (set `ODOO_UPDATE_MODULES=daisydo_agents` on the Railway Odoo service + redeploy,
   then clear it — or use *Apps → Daisy Agency → Upgrade*). Modules do **not**
   auto-deploy on push.

2. **Create the template chatflow** on Daisy+ (`Agent Template (Odoo MCP)`):
   - Type `AGENTFLOW`, model `chatDaisy:gpt-4o-mini`.
   - One `customMCP` tool node:
     - `url`: `__MCP_SERVER_URL__/mcp/sse`  ← must be the **/mcp/sse** SSE endpoint
     - headers: `{"X-Odoo-Username": "__ODOO_USERNAME__", "X-Odoo-API-Key": "__ODOO_API_KEY__"}`
     - `mcpActions`: the list of Odoo MCP tool names (from the MCP server's tool list).
   - The four placeholder tokens `__ODOO_URL__`, `__ODOO_USERNAME__`,
     `__ODOO_API_KEY__`, `__MCP_SERVER_URL__` are substituted with each agent's
     real credentials at clone time. Leave them as-is in the template.

3. **Set the config parameters** from the table above
   (`daisy.template_chatflow_id`, `daisy.global_api_key`, etc.).

4. **Flip the switch:** `daisy.autocreate_agents = True`.

5. **Smoke-test** before relying on it — see §6.

---

## 4. Day-to-day: onboarding an employee

1. Onboard the employee normally (HR → new employee **with an internal user
   login**, or the `onboard-employee` flow). The agent provisions automatically
   the moment the employee has a linked internal user.
2. **Verify:** open *Daisy Agency → Agents*, find the new agent:
   - `Daisy+ Provisioning` = **Provisioned**
   - `Chatflow ID` populated
   - a `<Name> - Email` project exists with the employee as PM/follower.
3. If anything failed, the agent shows **Error** + a `provision_error` message —
   fix the cause and click **Re-provision on Daisy+**.

---

## 5. Using an agent

- **Call it (API):**
  ```bash
  curl -X POST https://daisy.plus/api/v1/prediction/<CHATFLOW_ID> \
    -H "Authorization: Bearer <AGENT_DAISY_API_KEY>" \
    -H "Content-Type: application/json" \
    -d '{"question":"Find one res.partner and return its name."}'
  ```
  `<CHATFLOW_ID>` = the agent's `Chatflow ID`; `<AGENT_DAISY_API_KEY>` = its
  `Daisy+ API Key`. The agent reads/writes Odoo through MCP **at the employee's
  permission level**.
- **Email inbox:** mail to `<agent-code>@letsgomint.us` is fetched by the
  catchall and lands as **tasks** in the `<Name> - Email` project.
- **Capabilities scale with the employee's Odoo groups.** A brand-new employee
  has only basic internal-user access, so the agent can do only what that
  employee can. Grant the employee more Odoo groups to widen the agent's reach.

---

## 6. Quick smoke-test

- Open a draft/existing agent → **Re-provision on Daisy+** → expect a fresh
  cloned chatflow + linked key, `provision_state = Provisioned`.
- Fire a prediction (see §5) → the agent should answer using its Odoo tools.
- Create a throwaway internal user **without** an employee → confirm **no** agent
  is created (guard works); then link an `hr.employee` → confirm the agent appears.

---

## 7. Access & administration

- **Agent records** (`daisy.agent`) are governed by the **Daisy Agency** privilege
  (opt-in, assigned in *Settings → Users*):
  - *User* = read · *Officer* = read/write/create · *Administrator* = full.
- **The employee does NOT get a Daisy Agency group** by default — they can see
  their own **inbox** (as PM/follower) but not the agent config record.
- **Pause everything:** `daisy.autocreate_agents = False`.
- **Disable one agent:** use *Suspend* / *Terminate* on the agent form.

---

## 8. Gotchas & ops notes

- **XML-RPC `write` is broken on this Odoo** (custom `rpc` addon vs. base_automation).
  `create`/`read`/`unlink` work over XML-RPC; for scripted **writes** use the web
  JSON-RPC path (`/web/session/authenticate` → `/web/dataset/call_kw`) with browser
  headers (Cloudflare 403s header-less requests).
- **MCP endpoint is `/mcp/sse`** (SSE), not `/mcp` (404). The `customMCP` node
  connects via SSE.
- **`agent.daisy_api_key` must equal the chatflow's linked key** or predictions
  return `buildChatflow - Unauthorized`.
- **Daisy+ `POST /apikey` returns the full key list** (not just the created key);
  the code matches the created entry by `keyName`.
- **Cost/scale:** one chatflow + one API key **per employee**, all drawing on a
  **single shared Daisy+ workspace credit balance** (no per-agent quota) — monitor
  credit burn as agents multiply.

---

## 9. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Agent `provision_state = error` | Read `provision_error`. Common: `daisy.template_chatflow_id` / workspace key unset, or Daisy+ unreachable. Click **Re-provision**. |
| Prediction 500 `customMCP failed to initialize … SSE 404` | MCP URL/transport wrong — template must use `…/mcp/sse`. |
| Prediction 500 `buildChatflow - Unauthorized` | `daisy_api_key` ≠ the chatflow's linked `apikeyid` key. |
| New user got no agent | User not linked to an `hr.employee` (by design), or `daisy.autocreate_agents` is off. |
| Agent answers but can't read/write a record | The employee's Odoo permissions are too narrow — grant more groups. |
