# Backfill: every user backing a daisy.agent joins the Daisy Agents fleet
# group. Provisioning only started assigning the group in 19.0.5.9.0, so
# agents hired before then are missing the marker.
#
# Archived agents are deliberately excluded (default active_test): the
# auto-provisioning incident that attached agents to real human accounts was
# remediated by archiving those records, and their humans must not be
# flagged as fleet members.

from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    if env.ref("daisydo_agents.group_daisy_agents", raise_if_not_found=False):
        env["daisy.agent"].search([("user_id", "!=", False)])._ensure_fleet_group()
