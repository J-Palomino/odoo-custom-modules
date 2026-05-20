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

    # Daily lifecycle cron
    if not Cron.search([('name', '=', 'PTL: Daily Deal Lifecycle')], limit=1):
        Cron.create({
            'name': 'PTL: Daily Deal Lifecycle',
            'model_id': day_model.id,
            'state': 'code',
            'code': 'model._cron_daily_lifecycle()',
            'interval_number': 1,
            'interval_type': 'days',
            'active': True,
            'priority': 50,
        })
        _logger.info('PTL daily lifecycle cron created')
    else:
        _logger.info('PTL daily lifecycle cron already exists')

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

    # Backfill market_id for existing PTL days (migration from unique(date)
    # to unique(date, market_id))
    az_region = env['mint.region'].search([('code', '=', 'AZ')], limit=1)
    if az_region:
        days_without_market = env['mint.ptl.day'].search([('market_id', '=', False)])
        if days_without_market:
            days_without_market.write({'market_id': az_region.id})
            _logger.info('Backfilled market_id=AZ for %d PTL days', len(days_without_market))
