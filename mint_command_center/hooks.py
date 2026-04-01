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
        'numbercall': -1,
        'active': True,
        'priority': 50,
    })
    _logger.info('PTL daily lifecycle cron created')
