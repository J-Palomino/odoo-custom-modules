# One-shot cleanup: archive the hand-made "Project / Link Tracker" menu that
# exists in the prod DB without an external id (so it can't be targeted by
# ref in data/link_tracker_menu_policy.xml like the stock menus). Menus that
# DO have an xmlid are skipped — those belong to modules and are policy-managed
# in the data file. No match (e.g. staging) is a no-op.

from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    menus = env["ir.ui.menu"].search(
        [("name", "=", "Link Tracker"), ("parent_id.name", "=", "Project")]
    )
    xmlids = menus.get_external_id()
    handmade = menus.filtered(lambda m: not xmlids.get(m.id))
    if handmade:
        handmade.write({"active": False})
