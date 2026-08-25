#!/usr/bin/env python3
"""
Provision the Dutchie Roster Backfill **Supervisor** daisy.plus agent.

THIS IS A LIVE PRODUCTION ACTION. It creates a real bot user + scoped MCP key,
clones a chatflow, mints a Daisy+ key, and creates a scheduled task. It is NOT
loaded by Odoo (lives under docs/). Run it manually only after the backfill is
deployed and seeded (see supervisor-agent.md → Activation checklist), and only
with PROVISION_CONFIRM=yes in the environment.

It is a STARTING POINT, not a guaranteed one-shot: daisy.plus chatflow internals
(the agent's chatflow-id field, the Agentflow-v2 customMCP node shape, the
mcpActions allowlist) are version-dependent and finicky — verify each step's
response as you go (see memory: daisy-plus-provisioning, agent-key-must-match-
chatflow, stale-mcpActions-allowlist).

Env:
  ODOO_URL, ODOO_DB, ODOO_USERNAME, ODOO_API_KEY   # prod Odoo (XML-RPC)
  DAISY_API_BASE   = https://daisy.plus/api/v1
  DAISY_WORKSPACE_KEY                              # daisy_bot.api_key (chatflow edits)
  DAISY_GLOBAL_KEY                                 # daisy.global_api_key (scheduled-tasks)
  MONITORING_CHANNEL_ID                            # discuss.channel id to post digests to
  PROVISION_CONFIRM=yes                            # required guard
"""
import os
import sys
import xmlrpc.client

import requests

AGENT_CODE = 'dutchie-backfill-supervisor'
AGENT_NAME = 'Dutchie Roster Backfill Supervisor'
CRON_EXPR = '0 0 */2 * * *'  # 6-field quartz, UTC — every 2 hours
ODOO_MCP_ACTIONS = ['search_records', 'send_discuss_message']

SYSTEM_PROMPT = """\
You are the Dutchie Roster Backfill Supervisor. You MONITOR a customer-data
backfill; you never run or modify it.

When you receive the input "Scheduled task execution", do EXACTLY this, once:
1. Call the Odoo MCP tool `search_records` ONCE: model
   "mint.dutchie.sync.checkpoint", fields
   ["name","loc_id","state","rows_done","rows_total","last_run","last_error"],
   domain []. Do NOT make per-record calls (hard 10-tool-call limit).
2. Compute counts by state (done/running/pending/error), total rows_done, and
   the list of error stores with loc_id + last_error (truncate to ~160 chars).
3. Call the Odoo MCP tool `send_discuss_message` ONCE: channel_id =
   {channel_id}, body = a concise digest of those numbers.
Rules: read-only; exactly one search_records + one send_discuss_message per
fire; if no checkpoints, post "no checkpoints found — backfill not provisioned
yet"; report numbers verbatim, never invent counts or a date.
""".replace('{channel_id}', os.environ.get('MONITORING_CHANNEL_ID', '<SET_ME>'))


def _require(name):
    v = os.environ.get(name)
    if not v:
        sys.exit("Missing required env var: %s" % name)
    return v


def main():
    if os.environ.get('PROVISION_CONFIRM') != 'yes':
        sys.exit("Refusing to run: set PROVISION_CONFIRM=yes to provision (LIVE).")

    url = _require('ODOO_URL')
    db = _require('ODOO_DB')
    user = _require('ODOO_USERNAME')
    key = _require('ODOO_API_KEY')
    daisy_base = os.environ.get('DAISY_API_BASE', 'https://daisy.plus/api/v1')
    ws_key = _require('DAISY_WORKSPACE_KEY')
    global_key = _require('DAISY_GLOBAL_KEY')
    if not os.environ.get('MONITORING_CHANNEL_ID'):
        sys.exit("Set MONITORING_CHANNEL_ID (discuss.channel id for digests).")

    common = xmlrpc.client.ServerProxy('%s/xmlrpc/2/common' % url)
    uid = common.authenticate(db, user, key, {})
    if not uid:
        sys.exit("Odoo auth failed.")
    models = xmlrpc.client.ServerProxy('%s/xmlrpc/2/object' % url)

    def execute(model, method, *args):
        return models.execute_kw(db, uid, key, model, method, list(args))

    # 1. Functional create + hire (idempotent: reuse if the code already exists).
    existing = execute('daisy.agent', 'search', [[['code', '=', AGENT_CODE]]])
    if existing:
        agent_id = existing[0]
        print("Agent already exists: id=%s (skipping create/hire)" % agent_id)
    else:
        agent_id = execute('daisy.agent', 'create', [{
            'name': AGENT_NAME,
            'code': AGENT_CODE,
            'email': '%s@letsgomint.us' % AGENT_CODE,
            'manager_id': 2,
            'role': 'Backfill Supervisor',
            'bio': 'Monitors the Dutchie customer-roster backfill and posts digests.',
            'state': 'draft',
        }])
        print("Created daisy.agent id=%s; hiring..." % agent_id)
        execute('daisy.agent', 'action_hire', [[agent_id]])  # UI action; writes commit

    agent = execute('daisy.agent', 'read', [[agent_id]])[0]
    print("Agent record fields:", sorted(agent.keys()))
    # VERIFY at runtime: chatflow-id + agent-key field names vary by version.
    chatflow_id = agent.get('daisy_chatflow_id') or agent.get('chatflow_id')
    print("chatflow_id =", chatflow_id, "| provision_state =", agent.get('provision_state'))
    if not chatflow_id:
        sys.exit("Could not resolve the agent's chatflow id from the record — "
                 "inspect the printed fields and set it explicitly.")

    # 2. Set the system prompt + ensure the Odoo MCP allowlist on the chatflow.
    #    flowData is a JSON STRING; the Agent node holds agentMessages (system) and
    #    the customMCP tool's agentSelectedToolConfig.mcpActions. Edit carefully and
    #    re-read to confirm (see daisy-plus-provisioning for the node walk).
    cf = requests.get('%s/chatflows/%s' % (daisy_base, chatflow_id),
                      headers={'Authorization': 'Bearer %s' % ws_key}, timeout=30)
    cf.raise_for_status()
    print("Fetched chatflow; apply the system prompt + mcpActions=%s to its Agent "
          "node's agentMessages / agentSelectedToolConfig, then PUT it back:\n"
          "  PUT %s/chatflows/%s  {flowData: <updated json string>}\n"
          "  System prompt to install:\n%s"
          % (ODOO_MCP_ACTIONS, daisy_base, chatflow_id, SYSTEM_PROMPT))
    # NOTE: the precise flowData mutation is left explicit (not blindly scripted)
    # because a wrong node edit silently disables the agent's tools.

    # 3. Create the scheduled task (fires the FIXED "Scheduled task execution").
    st = requests.post('%s/scheduled-tasks' % daisy_base,
                       headers={'Authorization': 'Bearer %s' % global_key},
                       json={
                           'name': 'Dutchie backfill supervisor (2h)',
                           'cronExpression': CRON_EXPR,
                           'agentflowId': chatflow_id,
                           'isActive': True,
                       }, timeout=30)
    print("scheduled-task POST -> %s %s" % (st.status_code, st.text[:300]))
    print("\nDONE. Verify a manual fire posts a digest to the channel.")


if __name__ == '__main__':
    main()
