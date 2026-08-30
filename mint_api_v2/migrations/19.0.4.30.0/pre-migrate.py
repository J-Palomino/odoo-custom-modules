"""
Pre-migration for 19.0.4.30.0 — belt and braces for product_template.strain_id.

fix-config.sh already creates this column on every boot, on its own autocommit
connection, BEFORE Odoo starts — that is the real guarantee, because a
migrations/ ALTER runs inside the upgrade transaction and a later ParseError
would roll it back along with everything else. See the mint.strain block there
and the 2026-08-27 incident it references.

This exists for environments that run the module upgrade without that
entrypoint (a local checkout, a test harness), so the column is present before
the ORM registers the stored field either way. Idempotent.
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
    cr.execute("""
        CREATE INDEX IF NOT EXISTS product_template_strain_id_index
        ON product_template (strain_id)
    """)
    _logger.info("mint_api_v2 19.0.4.30.0 pre-migrate: product_template.strain_id ready")
