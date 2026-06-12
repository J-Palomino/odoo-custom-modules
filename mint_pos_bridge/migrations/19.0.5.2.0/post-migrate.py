# -*- coding: utf-8 -*-
"""Re-enable the stale-order cleanup cron on existing installs.

`cron_cancel_stale_orders` lives in a `noupdate="1"` data block and was manually
deactivated in prod on 2026-05-19 (it fired customer pushes + Dutchie webhooks on
every cancelled row — now fixed: the cron writes silently). Because the record is
noupdate, the XML's `active=True` won't re-apply on upgrade, so flip it here.

Idempotent — a no-op when the cron is already active.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    from odoo import api, SUPERUSER_ID

    env = api.Environment(cr, SUPERUSER_ID, {})
    cron = env.ref('mint_pos_bridge.cron_cancel_stale_orders', raise_if_not_found=False)
    if cron and not cron.active:
        cron.active = True
        _logger.info('mint_pos_bridge: re-enabled cron_cancel_stale_orders')
