# Backfill: every user linked to a daisy.agent joins the Daisy Agents fleet
# group. Provisioning only started assigning the group in 19.0.5.9.0, so
# agents hired before then are missing the marker.

from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    group = env.ref("daisydo_agents.group_daisy_agents", raise_if_not_found=False)
    if not group:
        return
    agents = (
        env["daisy.agent"]
        .with_context(active_test=False)
        .search([("user_id", "!=", False)])
    )
    users = agents.mapped("user_id").filtered("active") - group.user_ids
    if users:
        users.write({"group_ids": [(4, group.id)]})
