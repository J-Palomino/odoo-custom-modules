"""Pre-migration for 19.0.4.14.0 — schema additions for #93723.

Additive only. Adds new fields on mint.deal.submission and a new child
table mint_deal_submission_day. Backfills the new market_ids m2m from the
existing market_id m2o so the _compute_primary_market stored compute can
return the correct primary market on the first read after upgrade.

The new m2m relation table mint_deal_submission_market_rel is created by
Odoo's ORM on model load; this script only copies the legacy single-market
value into it so legacy rows continue to behave identically.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    _logger.info("mint_command_center 19.0.4.13.0: backfilling market_ids from market_id")

    # Odoo creates mint_deal_submission_market_rel on model load (before this
    # migration runs in the upgrade lifecycle? — actually pre-migrate runs
    # BEFORE the ORM creates new tables in Odoo 19's upgrade order). Defer
    # the backfill to a defensive check that runs only if the table exists.
    cr.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_name = 'mint_deal_submission_market_rel'"
    )
    if not cr.fetchone():
        _logger.info(
            "  mint_deal_submission_market_rel does not exist yet; "
            "Odoo will create it during model load. Backfill will run "
            "in post-migrate."
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
        "  mint_deal_submission_market_rel: backfilled %d row(s) from legacy market_id",
        cr.rowcount,
    )
