"""
Pre-migration for 19.0.6.71.0 — reclaim the PTL daily lifecycle cron.

"PTL: Daily Deal Lifecycle" was only ever created by hooks.py post_init_hook,
which runs on INSTALL and not on -u. Its guard matches by name regardless of
`active`, so once the cron was switched off nothing in the codebase could turn
it back on. It was disabled on production on 2026-06-18; by 2026-08-25 that had
left 329 PTL discounts still published past their valid_until (96 of them still
computing is_active=True), because unpublishing expired discounts is this
cron's job and nothing else does it — cron_expire_past_deals only flips
mint.ptl.deal.state.

data/ptl_cron_data.xml now defines the cron as a proper noupdate="1" data
record so it survives future upgrades. This migration runs BEFORE that file
loads and binds the existing cron to the new external id, so the upgrade adopts
the record already on the database instead of creating a second one alongside
it. Because the XML record is noupdate, the re-activation written here is what
survives the load.

Idempotent: re-running finds the xml_id already present and only re-asserts
active. On a database that never had the programmatic cron this is a no-op and
the XML record is created active by the normal data load.
"""
import logging

from odoo import api, SUPERUSER_ID, fields

_logger = logging.getLogger(__name__)

CRON_NAME = 'PTL: Daily Deal Lifecycle'
XMLID_MODULE = 'mint_command_center'
XMLID_NAME = 'ir_cron_ptl_daily_lifecycle'


def migrate(cr, version):
    if not version:
        return

    env = api.Environment(cr, SUPERUSER_ID, {})

    # active_test=False: the whole point is that this cron is switched off.
    crons = env['ir.cron'].with_context(active_test=False).search(
        [('name', '=', CRON_NAME)], order='id',
    )
    if not crons:
        _logger.info(
            'PTL lifecycle cron not present — XML data record will create it.')
        return
    if len(crons) > 1:
        _logger.warning(
            'Found %d crons named %r; adopting the oldest (id=%s) and leaving '
            'the rest untouched for manual review.',
            len(crons), CRON_NAME, crons[0].id,
        )
    cron = crons[0]
    was_active = cron.active

    existing = env['ir.model.data'].search([
        ('module', '=', XMLID_MODULE),
        ('name', '=', XMLID_NAME),
    ], limit=1)
    if existing:
        if existing.res_id != cron.id:
            _logger.warning(
                'xml_id %s.%s already points at ir.cron %s, not %s — leaving it '
                'alone.', XMLID_MODULE, XMLID_NAME, existing.res_id, cron.id,
            )
    else:
        env['ir.model.data'].create({
            'module': XMLID_MODULE,
            'name': XMLID_NAME,
            'model': 'ir.cron',
            'res_id': cron.id,
            'noupdate': True,
        })
        _logger.info('Adopted ir.cron %s as %s.%s', cron.id, XMLID_MODULE, XMLID_NAME)

    # nextcall is still sitting at the day after it was switched off, which
    # would make Odoo fire it once immediately. That is the behaviour we want
    # (there is a backlog to clear) but set it explicitly so the intent is not
    # an accident of the stale value.
    cron.write({'active': True, 'nextcall': fields.Datetime.now()})
    _logger.info(
        'PTL lifecycle cron id=%s re-activated (was active=%s).', cron.id, was_active)
