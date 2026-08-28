"""
Post-migration for 19.0.4.30.0 — phase 2 schema only. Does NOT back-link.

Linking rewrites strain_id on tens of thousands of templates. As with the
phase-1 seed, that is run by hand so its before/after numbers can be read back
rather than landing unobserved inside a deploy:

    env['mint.strain'].link_products_to_masters()   # -> rows changed
    env['mint.strain'].refresh_product_counts()     # -> total linked

Both are idempotent. Confirm the upgrade actually applied by checking
ir.module.module.latest_version == 19.0.4.30.0 — a green Railway deploy is not
evidence; this module has twice reported SUCCESS while silently rolling back.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    cr.execute("SELECT COUNT(*) FROM mint_strain")
    strains = cr.fetchone()[0]
    cr.execute("""
        SELECT COUNT(*) FROM product_template
         WHERE strain IS NOT NULL AND strain <> ''
    """)
    populated = cr.fetchone()[0]
    cr.execute("SELECT COUNT(*) FROM product_template WHERE strain_id IS NOT NULL")
    linked = cr.fetchone()[0]

    _logger.warning(
        "mint_api_v2 19.0.4.30.0 (phase 2): strain_id installed, NOT back-linked. "
        "%s masters, %s templates carry strain text, %s already linked. "
        "Run env['mint.strain'].link_products_to_masters().",
        strains, populated, linked,
    )
