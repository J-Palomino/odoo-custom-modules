# Daily deal→product resolver — one-time setup (idempotent).
# ==========================================================
# Run in an Odoo shell after upgrading mint_command_center:
#   odoo shell -d <db> --no-http < mint_command_center/scripts/setup_deal_resolver.py
#
# Wires the config params + a log channel for the resolver cron
# (mint.ptl.deal._cron_resolve_deal_products). The cron defaults to DIRECT mode
# (calls mintinvsvc itself) so daily resolution works immediately; flip to AGENT
# mode once a "Deal Product Resolver" daisy agent is provisioned.

ICP = env["ir.config_parameter"].sudo()

# 1. Resolver endpoint (mintinvsvc) — default is correct; override only if needed.
ICP.set_param(
    "mint.deal_resolver.url",
    "https://mintinvsvc-production-6aa5.up.railway.app/api/jobs/resolve-deal-products",
)

# 2. mintinvsvc x-api-key is shared with the PTL sync param. Warn if unset.
if not ICP.get_param("mint.inventory_service.api_key"):
    print("WARN: mint.inventory_service.api_key is empty — set it to the mintinvsvc x-api-key (REDIS_API_KEY).")

# 3. A Discuss channel for the agent to post run summaries to.
Channel = env["discuss.channel"].sudo()
ch = Channel.search([("name", "=", "🤖 Deal Resolver")], limit=1)
if not ch:
    ch = Channel.create({"name": "🤖 Deal Resolver", "channel_type": "channel"})
ICP.set_param("mint.deal_resolver.channel_id", str(ch.id))
print("Channel:", ch.id)

# 4. Point at the daisy agent if one exists; else stay DIRECT (deterministic).
agent = None
if "daisy.agent" in env:
    agent = env["daisy.agent"].search([("name", "=", "Deal Product Resolver")], limit=1)
if agent:
    ICP.set_param("mint.deal_resolver.agent_id", str(agent.id))
    ICP.set_param("mint.deal_resolver.mode", "agent")
    # Make sure the agent's bot user is in the channel so it can post.
    if agent.user_id and agent.user_id.partner_id:
        ch.sudo().add_members(partner_ids=agent.user_id.partner_id.ids)
    print("AGENT mode — agent", agent.id, agent.name)
    print('Set the agent chatflow instruction: "When asked to resolve daily-deal '
          'products, call Odoo mint.ptl.deal.action_resolve_products (model-level) '
          'and report the returned summary."')
else:
    ICP.set_param("mint.deal_resolver.mode", "direct")
    print("DIRECT mode (deterministic) — no 'Deal Product Resolver' agent found yet.")
    print("To switch to a daisy agent later: provision one via Daisy ▸ Agents (give it the")
    print("Odoo MCP), add it to the channel, then set mint.deal_resolver.agent_id and")
    print("mint.deal_resolver.mode='agent'.")

env.cr.commit()
print("Deal resolver setup complete. Cron: 'Daily Deals: resolve deal→product ids'.")
