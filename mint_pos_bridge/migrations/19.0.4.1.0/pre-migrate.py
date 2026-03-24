# -*- coding: utf-8 -*-
"""Pre-migration: fix dutchie_checkout_id unique constraint.

The old UNIQUE(company_id, dutchie_checkout_id) constraint fails because
walk-in POS orders store '' instead of NULL, and PostgreSQL UNIQUE treats
multiple '' as duplicates. This migration:

1. Converts empty-string dutchie_checkout_id values to NULL
2. Drops the old constraint/index so the module can recreate it as a
   partial unique index (WHERE dutchie_checkout_id IS NOT NULL AND != '')
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    _logger.info(
        "mint_pos_bridge pre-migrate %s: fixing dutchie_checkout_id constraint",
        version,
    )

    # 1. Set empty strings to NULL
    cr.execute("""
        UPDATE mint_pos_order
        SET dutchie_checkout_id = NULL
        WHERE dutchie_checkout_id = ''
    """)
    updated = cr.rowcount
    _logger.info("Set %d empty dutchie_checkout_id values to NULL", updated)

    # 2. Drop old unique constraint (could be a constraint or an index)
    # Try dropping as a table constraint first
    cr.execute("""
        SELECT conname FROM pg_constraint
        WHERE conrelid = 'mint_pos_order'::regclass
          AND conname = 'mint_pos_order_dutchie_checkout_uniq'
    """)
    if cr.fetchone():
        cr.execute("""
            ALTER TABLE mint_pos_order
            DROP CONSTRAINT mint_pos_order_dutchie_checkout_uniq
        """)
        _logger.info("Dropped old table constraint mint_pos_order_dutchie_checkout_uniq")
    else:
        # Try dropping as an index
        cr.execute("""
            DROP INDEX IF EXISTS mint_pos_order_dutchie_checkout_uniq
        """)
        _logger.info("Dropped old index mint_pos_order_dutchie_checkout_uniq (if existed)")

    # Also drop the auto-generated Odoo constraint name variant
    cr.execute("""
        SELECT conname FROM pg_constraint
        WHERE conrelid = 'mint_pos_order'::regclass
          AND conname LIKE '%dutchie_checkout%uniq%'
    """)
    for row in cr.fetchall():
        cr.execute(
            "ALTER TABLE mint_pos_order DROP CONSTRAINT %s" % row[0]
        )
        _logger.info("Dropped constraint %s", row[0])
