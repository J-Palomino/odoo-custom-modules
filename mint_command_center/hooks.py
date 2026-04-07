import logging

_logger = logging.getLogger(__name__)


def post_init_hook(env):
    """Create the PTL daily lifecycle cron if it doesn't exist."""
    Cron = env['ir.cron'].sudo()
    existing = Cron.search([('name', '=', 'PTL: Daily Deal Lifecycle')], limit=1)
    if existing:
        _logger.info('PTL cron already exists (id=%s)', existing.id)
        return

    model = env['ir.model'].sudo().search([('model', '=', 'mint.ptl.day')], limit=1)
    if not model:
        _logger.warning('mint.ptl.day model not found — skipping cron creation')
        return

    Cron.create({
        'name': 'PTL: Daily Deal Lifecycle',
        'model_id': model.id,
        'state': 'code',
        'code': 'model._cron_daily_lifecycle()',
        'interval_number': 1,
        'interval_type': 'days',
        'active': True,
        'priority': 50,
    })
    _logger.info('PTL daily lifecycle cron created')

    # Backfill market_id for existing PTL days (migration from unique(date)
    # to unique(date, market_id))
    az_region = env['mint.region'].search([('code', '=', 'AZ')], limit=1)
    if az_region:
        days_without_market = env['mint.ptl.day'].search([('market_id', '=', False)])
        if days_without_market:
            days_without_market.write({'market_id': az_region.id})
            _logger.info('Backfilled market_id=AZ for %d PTL days', len(days_without_market))
