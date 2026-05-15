"""
Pre-migration for 19.0.4.0.0 — National Promo Log + is_active/is_published split prep.

Phase 1 of the split:
  - Adds `is_published` column on `mint_discount` and seeds it with current
    `is_active` values. `is_active` stays a real Boolean for this version;
    it becomes a stored compute in 19.0.4.1.0.
  - Backfills 24/7 day-of-week booleans on legacy dutchie/manual discounts so
    the future compute returns True for them (they were never explicitly dow-scoped).
  - Drops the legacy `unique(brand_id, date, market_id)` constraint on
    `mint_brand_calendar_entry` so the new SKU-level uniqueness can take effect.

Phase 1 is intentionally additive — nothing is renamed yet; existing consumers
of `is_active` continue to work unchanged.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    _logger.info("mint_command_center 19.0.4.0.0: prepping National Promo Log + is_published")

    # 1. Add is_published column on mint_discount (idempotent)
    cr.execute(
        """
        ALTER TABLE mint_discount
        ADD COLUMN IF NOT EXISTS is_published boolean DEFAULT false
        """
    )
    cr.execute(
        """
        UPDATE mint_discount
           SET is_published = is_active
         WHERE is_published IS DISTINCT FROM is_active
        """
    )
    cr.execute("SELECT COUNT(*) FROM mint_discount WHERE is_published = true")
    seeded = cr.fetchone()[0]
    _logger.info("  is_published seeded: %d records mirror current is_active", seeded)

    # 2. Default 24/7 dow for legacy dutchie/manual rows where dow was never set
    cr.execute(
        """
        UPDATE mint_discount
           SET monday=true, tuesday=true, wednesday=true, thursday=true,
               friday=true, saturday=true, sunday=true
         WHERE source IN ('dutchie', 'manual')
           AND (monday IS NOT TRUE
                AND tuesday IS NOT TRUE
                AND wednesday IS NOT TRUE
                AND thursday IS NOT TRUE
                AND friday IS NOT TRUE
                AND saturday IS NOT TRUE
                AND sunday IS NOT TRUE)
        """
    )
    _logger.info("  legacy dow bools backfilled: %d records", cr.rowcount)

    # 3. Drop the old brand_calendar constraint so SKU dim is allowed
    cr.execute(
        """
        SELECT conname FROM pg_constraint
         WHERE conrelid = 'mint_brand_calendar_entry'::regclass
           AND contype = 'u'
           AND conname LIKE '%brand_date_market%'
        """
    )
    for (conname,) in cr.fetchall():
        _logger.info("  dropping old unique constraint %s", conname)
        cr.execute(f"ALTER TABLE mint_brand_calendar_entry DROP CONSTRAINT IF EXISTS {conname}")

    _logger.info("mint_command_center 19.0.4.0.0: pre-migrate complete")
