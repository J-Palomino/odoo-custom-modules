"""
Post-migration for 19.0.5.2.0 — backfill mint.deal.submission.option.

Phase 3 introduces mint.deal.submission.option as the source of truth for
discount specs on a submission; discount_type / discount_value on the
parent become computed-stored mirrors of the first option.

Same trick as the 19.0.5.1.0 migration: read legacy values via raw SQL
before any ORM call. The newly stored-computed field is initialized at
module load with option_ids empty, so a normal Submission.search would
see post-init zeroes and the backfill would silently no-op.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    cr.execute("""
        SELECT s.id, s.discount_type, s.discount_value
        FROM mint_deal_submission s
        WHERE s.discount_type IS NOT NULL
          AND s.discount_type <> ''
          AND NOT EXISTS (
            SELECT 1
            FROM mint_deal_submission_option o
            WHERE o.submission_id = s.id
          )
    """)
    rows = cr.fetchall()

    if not rows:
        _logger.info(
            'mint_command_center 19.0.5.2.0 post-migrate: '
            'no submissions need option backfill'
        )
        return

    _logger.info(
        'mint_command_center 19.0.5.2.0 post-migrate: '
        'backfilling %d deal.submission records with an initial option',
        len(rows),
    )

    from odoo import api, SUPERUSER_ID
    env = api.Environment(cr, SUPERUSER_ID, {})
    Submission = env['mint.deal.submission']
    Option = env['mint.deal.submission.option']

    vals_list = [
        Submission.browse(sub_id)._primary_option_vals(discount_type, discount_value)
        for sub_id, discount_type, discount_value in rows
    ]
    Option.create(vals_list)
    _logger.info('mint_command_center 19.0.5.2.0 post-migrate: backfill complete')
