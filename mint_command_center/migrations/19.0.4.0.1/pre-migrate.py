"""
Pre-migration for 19.0.4.0.1 — null orphan FKs that block FK constraint
re-creation during module upgrade.

The 19.0.4.0.0 upgrade attempted to add `mint_ptl_deal_brand_id_fkey` and
failed because at least one row in `mint_ptl_deal` referenced a `brand_id`
not present in `mint_brand` (e.g. the brand was hard-deleted out of band,
not via Odoo). Set those orphan brand_id values to NULL so the FK can be
created cleanly.

Same defensive pass for `mint_brand_calendar_entry.brand_id`, since the
new brand-calendar shape adds the same constraint.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    _logger.info("mint_command_center 19.0.4.0.1: nulling orphan brand FKs")

    # mint_ptl_deal.brand_id orphans
    cr.execute("""
        UPDATE mint_ptl_deal
           SET brand_id = NULL
         WHERE brand_id IS NOT NULL
           AND brand_id NOT IN (SELECT id FROM mint_brand)
    """)
    _logger.info("  mint_ptl_deal: nulled %d orphan brand_id rows", cr.rowcount)

    # mint_brand_calendar_entry.brand_id orphans (same safety net)
    cr.execute("""
        SELECT 1 FROM information_schema.tables
         WHERE table_name = 'mint_brand_calendar_entry'
    """)
    if cr.fetchone():
        cr.execute("""
            UPDATE mint_brand_calendar_entry
               SET brand_id = NULL
             WHERE brand_id IS NOT NULL
               AND brand_id NOT IN (SELECT id FROM mint_brand)
        """)
        _logger.info(
            "  mint_brand_calendar_entry: nulled %d orphan brand_id rows",
            cr.rowcount,
        )

    # m2m excluded brands — drop orphan rel rows entirely
    cr.execute("""
        SELECT 1 FROM information_schema.tables
         WHERE table_name = 'mint_ptl_deal_excluded_brand_rel'
    """)
    if cr.fetchone():
        cr.execute("""
            DELETE FROM mint_ptl_deal_excluded_brand_rel
             WHERE brand_id NOT IN (SELECT id FROM mint_brand)
        """)
        _logger.info(
            "  mint_ptl_deal_excluded_brand_rel: dropped %d orphan rows",
            cr.rowcount,
        )

    _logger.info("mint_command_center 19.0.4.0.1: pre-migrate complete")
