# -*- coding: utf-8 -*-
"""Self-heal the POS-app visibility link on existing installs.

`group_pos_budtender` was first created under a `noupdate="1"` data block, so the
`implied_ids` added in security_groups.xml are skipped on upgrade (Odoo honors the
stored `ir.model.data.noupdate` flag, regardless of the current block's attribute).

This post-migration links the group to `point_of_sale.group_pos_user` directly, so
every existing install (prod, staging, restored backups) self-heals: the swimlane
board now nests under the native Point of Sale app, whose root menu is only visible
to POS users — budtenders must hold `point_of_sale.group_pos_user` to see it.

Idempotent — a no-op when the link already exists.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    from odoo import api, SUPERUSER_ID

    env = api.Environment(cr, SUPERUSER_ID, {})
    budtender = env.ref('mint_pos_bridge.group_pos_budtender', raise_if_not_found=False)
    pos_user = env.ref('point_of_sale.group_pos_user', raise_if_not_found=False)
    if not budtender or not pos_user:
        _logger.warning(
            'mint_pos_bridge: skipping implied-group link — '
            'group_pos_budtender=%s point_of_sale.group_pos_user=%s',
            budtender, pos_user,
        )
        return
    if pos_user in budtender.implied_ids:
        return
    budtender.implied_ids = [(4, pos_user.id)]
    _logger.info(
        'mint_pos_bridge: linked group_pos_budtender -> point_of_sale.group_pos_user'
    )
