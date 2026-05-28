"""
Post-migration for 19.0.5.1.0 — backfill mint.ptl.deal.option for legacy deals.

Phase 1 (19.0.5.0.0) introduced mint.ptl.deal.option as an additive child.
Phase 2 (this version) makes mint.ptl.deal.discount_type/discount_value
computed mirrors of the first option, so every existing deal needs at least
one option row to keep the mirror correct.

For each mint.ptl.deal with a non-empty discount_type and no options yet,
create one option carrying the deal's current discount_type/discount_value.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    from odoo import api, SUPERUSER_ID

    env = api.Environment(cr, SUPERUSER_ID, {})
    Deal = env['mint.ptl.deal']
    Option = env['mint.ptl.deal.option']

    candidates = Deal.search([('discount_type', '!=', False)])
    to_backfill = candidates.filtered(lambda d: not d.option_ids)

    if not to_backfill:
        _logger.info(
            'mint_command_center 19.0.5.1.0 post-migrate: '
            'no deals need option backfill (checked %d)', len(candidates),
        )
        return

    _logger.info(
        'mint_command_center 19.0.5.1.0 post-migrate: '
        'backfilling %d ptl.deal records with an initial option',
        len(to_backfill),
    )

    vals_list = [{
        'ptl_deal_id': deal.id,
        'sequence': 10,
        'discount_type': deal.discount_type,
        'discount_value': deal.discount_value or 0.0,
    } for deal in to_backfill]

    Option.create(vals_list)
    _logger.info('mint_command_center 19.0.5.1.0 post-migrate: backfill complete')
