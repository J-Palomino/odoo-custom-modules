"""
Pre-migration for 19.0.4.28.0 — ensure product_template.strain_id exists.

Same rationale as the 19.0.4.17.0 pre-migrate for mint_brand.product_count:
`_auto_init` would normally add the column during upgrade, but XML data loads
in this module have rolled back the upgrade transaction before now, taking the
schema change with them. Creating it here, before any data load, means the
post-migrate seed always has a column to write into.

mint_strain itself is a brand-new table — Odoo creates it during registry init,
so there is nothing to pre-create for it.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    cr.execute("""
        ALTER TABLE product_template
        ADD COLUMN IF NOT EXISTS strain_id INTEGER
    """)
    # Index matches the ORM field's index=True; created here so the post-migrate
    # bulk UPDATE and the product list's strain filter are not seq-scans.
    cr.execute("""
        CREATE INDEX IF NOT EXISTS product_template_strain_id_index
        ON product_template (strain_id)
    """)
    _logger.info("mint_api_v2 19.0.4.28.0 pre-migrate: product_template.strain_id ready")
