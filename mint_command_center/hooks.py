import logging

_logger = logging.getLogger(__name__)


def post_init_hook(env):
    """Create PTL crons if they don't exist."""
    Cron = env['ir.cron'].sudo()

    day_model = env['ir.model'].sudo().search([('model', '=', 'mint.ptl.day')], limit=1)
    discount_model = env['ir.model'].sudo().search([('model', '=', 'mint.discount')], limit=1)

    if not day_model:
        _logger.warning('mint.ptl.day model not found — skipping cron creation')
        return

    # NOTE: "PTL: Daily Deal Lifecycle" is deliberately NOT created here any
    # more. It lives in data/ptl_cron_data.xml as of 19.0.6.71.0.
    #
    # Creating a recurring cron from post_init_hook is a trap: the hook runs on
    # install only, never on -u, and its name-based guard matched an existing
    # cron regardless of `active`. So when it was switched off on prod (2026-06-18)
    # nothing could ever switch it back on, and expired PTL discounts stopped
    # being unpublished for two months. A data record with noupdate="1" is
    # restorable by an upgrade; a hook-created record is not.

    # Hourly is_active recompute cron
    if discount_model and not Cron.search(
        [('name', '=', 'PTL: Hourly Active Recompute')], limit=1
    ):
        Cron.create({
            'name': 'PTL: Hourly Active Recompute',
            'model_id': discount_model.id,
            'state': 'code',
            'code': 'model._cron_recompute_active()',
            'interval_number': 1,
            'interval_type': 'hours',
            'active': True,
            'priority': 60,
        })
        _logger.info('PTL hourly active recompute cron created')
    elif discount_model:
        _logger.info('PTL hourly active recompute cron already exists')

    # Velocity compute cron (F7) — nightly aggregation of mint.pos.order.line
    # → product.template velocity fields.
    pt_model = env['ir.model'].sudo().search(
        [('model', '=', 'product.template')], limit=1)
    if pt_model and not Cron.search(
        [('name', '=', 'MCC: Sales Velocity Compute')], limit=1
    ):
        Cron.create({
            'name': 'MCC: Sales Velocity Compute',
            'model_id': pt_model.id,
            'state': 'code',
            'code': 'model._cron_compute_velocity()',
            'interval_number': 1,
            'interval_type': 'days',
            'active': True,
            'priority': 70,
        })
        _logger.info('Velocity compute cron created')
    elif pt_model:
        _logger.info('Velocity compute cron already exists')

    # Deal-submission lifecycle cron — advances Scheduled -> Final Review once
    # the run window has ended (#promos board lifecycle).
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
        _logger.info('Deal-submission lifecycle cron created')
    elif sub_model:
        _logger.info('Deal-submission lifecycle cron already exists')

    # Backfill market_id for existing PTL days (migration from unique(date)
    # to unique(date, market_id))
    az_region = env['mint.region'].search([('code', '=', 'AZ')], limit=1)
    if az_region:
        days_without_market = env['mint.ptl.day'].search([('market_id', '=', False)])
        if days_without_market:
            days_without_market.write({'market_id': az_region.id})
            _logger.info('Backfilled market_id=AZ for %d PTL days', len(days_without_market))
