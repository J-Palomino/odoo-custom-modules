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
