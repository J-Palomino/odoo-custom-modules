"""Post-migration for 19.0.4.14.0.

Two independent, idempotent backfills that collided on the same version number
on the main and staging lines — combined during the main→staging reconcile
(2026-06). Both are safe to re-run.

  1. mint_deal_submission_window      ← legacy preferred_start_date/end_date
                                        (main line)
  2. mint_deal_submission_market_rel  ← legacy single market_id, for the
                                        #93723 multi-market m2m (staging line)

`preferred_days` (free-text, e.g. "Mon, Wed, Fri") is intentionally NOT parsed
into windows — too lossy; it stays on the legacy field for staff reference.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    # 1. Back-fill mint.deal.submission.window from legacy preferred dates.
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
    _logger.info(
        "mint_command_center 19.0.4.14.0: back-filled %s window row(s)",
        cr.rowcount,
    )

    # 2. Back-fill the #93723 multi-market m2m from the legacy single market_id.
    #    The relation table only exists once mint.deal.submission.market_ids is
    #    loaded; guard so a partial upgrade doesn't crash.
    cr.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_name = 'mint_deal_submission_market_rel'"
    )
    if not cr.fetchone():
        _logger.warning(
            "mint_deal_submission_market_rel does not exist post-upgrade; "
            "skipping market backfill. Check the m2m field definition on "
            "mint.deal.submission.market_ids."
        )
        return

    cr.execute(
        """
        INSERT INTO mint_deal_submission_market_rel (submission_id, market_id)
        SELECT id, market_id
          FROM mint_deal_submission
         WHERE market_id IS NOT NULL
           AND id NOT IN (
               SELECT submission_id FROM mint_deal_submission_market_rel
           )
        """
    )
    _logger.info(
        "mint_command_center 19.0.4.14.0: backfilled %d row(s) into "
        "mint_deal_submission_market_rel from legacy market_id",
        cr.rowcount,
    )
