"""Clear company_id on shared equipment and clean orphaned filestore attachments."""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    # 1. Equipment used by the Fix-It forms (teams 1-4, 13, 14) should be
    #    company-agnostic so any store can reference them without triggering
    #    multi-company constraint violations.
    cr.execute("""
        UPDATE maintenance_equipment
        SET company_id = NULL
        WHERE maintenance_team_id IN (1, 2, 3, 4, 13, 14)
          AND company_id IS NOT NULL
    """)
    _logger.info(
        "Cleared company_id on %d shared equipment records",
        cr.rowcount,
    )

    # 2. Remove ir.attachment records pointing to missing filestore files.
    #    These cause repeated FileNotFoundError warnings in the logs.
    broken_hashes = [
        '76/76192501f035928742921584b02136689bba6932',
        '6f/6f61d1a0f95bc35496e324130cd3751ce53aa9fb',
        'e1/e13b7bc9a9321b2053a000cd04179f72cd836eac',
    ]
    cr.execute("""
        DELETE FROM ir_attachment
        WHERE store_fname = ANY(%s)
    """, (broken_hashes,))
    _logger.info(
        "Removed %d orphaned attachment records with missing filestore files",
        cr.rowcount,
    )

    # 3. Regenerate web asset bundles (clears stale compiled JS/CSS references)
    cr.execute("""
        DELETE FROM ir_attachment
        WHERE name LIKE '%%.assets_%%'
          AND res_model = 'ir.ui.view'
    """)
    _logger.info(
        "Cleared %d stale web asset bundle attachments (will regenerate on next request)",
        cr.rowcount,
    )
