"""Remap mint.deal.submission states to the /promos board lifecycle.

Old states: new / under_review / approved / rejected / converted
New states: new / under_review / approved / scheduled / final_review /
            expired / rejected

  converted -> scheduled   (a PTL deal was created + plotted = scheduled to
                            go live; the daily lifecycle cron later advances it
                            to Final Review once the run window ends)

'approved' and 'rejected' are unchanged. Raw SQL avoids ORM selection
validation on the now-removed 'converted' value. The new stored computed
`run_end_date` is populated automatically by Odoo when the field is
initialised during the upgrade.
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

    # post_init_hook only runs on a fresh INSTALL, so an existing (already
    # installed) instance being upgraded to this version would never get the
    # new lifecycle cron — leaving Scheduled deals stuck (no auto-advance to
    # Final Review). Create it here too, idempotently by name.
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
