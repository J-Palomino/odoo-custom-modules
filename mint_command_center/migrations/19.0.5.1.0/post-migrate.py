"""
Post-migration for 19.0.5.1.0 — backfill mint.ptl.deal.option for legacy deals.

Phase 1 (19.0.5.0.0) introduced mint.ptl.deal.option as an additive child.
Phase 2 (this version) makes mint.ptl.deal.discount_type/discount_value
computed mirrors of the first option, so every existing deal needs at least
one option row to keep the mirror correct.

Reads legacy discount_type/discount_value via raw SQL BEFORE invoking any
ORM call on mint.ptl.deal. The new stored-computed fields are initialized
during module load (before this script runs); a normal `Deal.search` would
see the post-init values (False/0.0, since option_ids is still empty) and
the backfill would silently no-op. Going to SQL preserves the pre-upgrade
values until we've written the corresponding options.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    # Single round-trip: pre-upgrade discount values for every deal that
    # had a discount_type set and does not yet have any option rows.
    cr.execute("""
        SELECT d.id, d.discount_type, d.discount_value
        FROM mint_ptl_deal d
        WHERE d.discount_type IS NOT NULL
          AND NOT EXISTS (
            SELECT 1
            FROM mint_ptl_deal_option o
            WHERE o.ptl_deal_id = d.id
          )
    """)
    rows = cr.fetchall()

    if not rows:
        _logger.info(
            'mint_command_center 19.0.5.1.0 post-migrate: '
            'no deals need option backfill'
        )
        return

    _logger.info(
        'mint_command_center 19.0.5.1.0 post-migrate: '
        'backfilling %d ptl.deal records with an initial option',
        len(rows),
    )

    from odoo import api, SUPERUSER_ID
    env = api.Environment(cr, SUPERUSER_ID, {})
    Deal = env['mint.ptl.deal']
    Option = env['mint.ptl.deal.option']

    vals_list = [
        Deal.browse(deal_id)._primary_option_vals(discount_type, discount_value)
        for deal_id, discount_type, discount_value in rows
    ]
    Option.create(vals_list)
    _logger.info('mint_command_center 19.0.5.1.0 post-migrate: backfill complete')
