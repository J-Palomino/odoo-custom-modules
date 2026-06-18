"""Remap mint.deal.submission states to the /promos board lifecycle, and
ensure the daily lifecycle cron exists on the upgrade path.

Old states: new / under_review / approved / rejected / converted
New states: new / under_review / approved / scheduled / final_review /
            expired / rejected

  converted -> scheduled  (a PTL deal was created + plotted = scheduled to go
                           live; the daily cron later advances it to Final
                           Review once the run window ends)

Re-tagged from 19.0.6.6.0 so it runs on the prod -> 6.19.0 upgrade — prod
never installed 6.6.0, so a 6.6.0-tagged migration would be skipped (prod is well past 6.6.0). The new
stored run_end_date is populated automatically by Odoo when the field is
initialised during the upgrade. post_init_hook only fires on a fresh INSTALL,
so the lifecycle cron is created here (idempotently) for the UPGRADE path.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute("""
        UPDATE mint_deal_submission
           SET state = 'scheduled'
         WHERE state = 'converted'
    """)
    _logger.info(
        'mint.deal.submission: remapped %d converted -> scheduled', cr.rowcount
    )

    env = api.Environment(cr, SUPERUSER_ID, {})
    Cron = env['ir.cron'].sudo()
    sub_model = env['ir.model'].sudo().search(
        [('model', '=', 'mint.deal.submission')], limit=1)
    if sub_model and not Cron.search(
        [('name', '=', 'Deal Submissions: Daily Lifecycle')], limit=1
    ):
        Cron.create({
            'name': 'Deal Submissions: Daily Lifecycle',
            'model_id': sub_model.id,
            'state': 'code',
            'code': 'model._cron_advance_lifecycle()',
            'interval_number': 1,
            'interval_type': 'days',
            'active': True,
            'priority': 55,
        })
        _logger.info('Deal-submission lifecycle cron created (upgrade path)')
    else:
        _logger.info('Deal-submission lifecycle cron already exists')
