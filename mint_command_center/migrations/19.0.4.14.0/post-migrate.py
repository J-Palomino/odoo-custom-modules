"""
Post-migration for 19.0.4.14.0 — back-fill mint.deal.submission.window
from legacy preferred_start_date / preferred_end_date.

Creates exactly one window row per submission that:
  - has both preferred_start_date AND preferred_end_date set
  - has no window_ids yet (idempotent — re-running won't dup)

`preferred_days` (free-text, e.g. "Mon, Wed, Fri") is intentionally NOT
parsed — too lossy. It stays on the legacy fields for staff reference.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    _logger.info(
        "mint_command_center 19.0.4.14.0: back-filling deal-submission windows"
    )

    cr.execute(
        """
        INSERT INTO mint_deal_submission_window
            (submission_id, sequence, date_start, date_end,
             create_uid, create_date, write_uid, write_date)
        SELECT s.id, 10, s.preferred_start_date, s.preferred_end_date,
               1, NOW() AT TIME ZONE 'UTC', 1, NOW() AT TIME ZONE 'UTC'
          FROM mint_deal_submission s
         WHERE s.preferred_start_date IS NOT NULL
           AND s.preferred_end_date IS NOT NULL
           AND s.preferred_start_date <= s.preferred_end_date
           AND NOT EXISTS (
               SELECT 1 FROM mint_deal_submission_window w
                WHERE w.submission_id = s.id
           )
        """
    )
    inserted = cr.rowcount
    _logger.info(
        "mint_command_center 19.0.4.14.0: back-filled %s window row(s)",
        inserted,
    )
