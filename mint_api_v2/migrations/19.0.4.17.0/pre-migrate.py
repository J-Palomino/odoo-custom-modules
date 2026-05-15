"""
Pre-migration for 19.0.4.16.0 — ensure mint_brand.product_count column exists.

The new field is defined on `class MintBrand` in product_template.py. Odoo's
`_auto_init` would normally create the DB column during module upgrade, but
other XML data loads in this module have been rolling back the upgrade
transaction, which also rolls back the schema change. This SQL runs before
any data load so the column lands regardless of what comes after.

Also recomputes product_count for all brands via raw SQL so domain-filtered
dropdowns have the right data on first open.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    cr.execute("""
        ALTER TABLE mint_brand
        ADD COLUMN IF NOT EXISTS product_count INTEGER DEFAULT 0
    """)

    # Detect the cannabis flag column name (renamed x_is_cannabis -> is_cannabis
    # at some point; environments may have either, both, or neither).
    cr.execute("""
        SELECT column_name FROM information_schema.columns
         WHERE table_name = 'product_template'
           AND column_name IN ('x_is_cannabis', 'is_cannabis')
         ORDER BY CASE column_name WHEN 'x_is_cannabis' THEN 1 ELSE 2 END
         LIMIT 1
    """)
    row = cr.fetchone()
    if not row:
        _logger.info("mint_api_v2: no cannabis flag column found; skipping product_count backfill")
        return
    cannabis_col = row[0]

    # Backfill product_count from product.template (cannabis products only).
    # Faster than letting Odoo compute one-by-one during ORM registration.
    cr.execute(f"""
        UPDATE mint_brand b SET product_count = COALESCE(c.cnt, 0)
        FROM (
          SELECT brand_id, COUNT(*) AS cnt
          FROM product_template
          WHERE brand_id IS NOT NULL AND {cannabis_col} = true
          GROUP BY brand_id
        ) c
        WHERE b.id = c.brand_id
    """)
    _logger.info("mint_api_v2: mint_brand.product_count ensured + backfilled (cannabis col=%s)", cannabis_col)
