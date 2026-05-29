"""Post-migration for 19.0.4.14.0 — backfill market_ids m2m for #93723.

Runs AFTER Odoo creates the new mint_deal_submission_market_rel table
(via ORM model load). Copies the legacy single market_id into the m2m
so the _compute_primary_market stored compute returns the correct
primary market on the next read.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_name = 'mint_deal_submission_market_rel'"
    )
    if not cr.fetchone():
        _logger.warning(
            "mint_deal_submission_market_rel still does not exist post-upgrade; "
            "skipping backfill. Check the m2m field definition on "
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
        "19.0.4.14.0 post-migrate: backfilled %d row(s) into "
        "mint_deal_submission_market_rel from legacy market_id",
        cr.rowcount,
    )
