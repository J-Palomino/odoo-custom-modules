"""
Post-migration for 19.0.4.28.0 — phase 1: mint.strain exists, unseeded.

Does NOT seed. The seed creates ~4,172 records from the catalog's own text and
is run by hand so its before/after numbers can be read back, rather than
landing unobserved inside a deploy. Seeding later is safe: seed_from_products()
is idempotent and folds new spellings into existing masters.

    env['mint.strain'].seed_from_products()      # -> summary dict

Phase 1 touches nothing on product.template, so if this upgrade is silently
skipped the only consequence is that mint.strain does not exist. Confirm it
actually ran by checking ir.module.module.latest_version == 19.0.4.28.0 — a
green Railway deploy is not evidence (PR #360 deployed SUCCESS while this
module stayed at 19.0.4.27.0).
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    cr.execute("""
        SELECT COUNT(*), COUNT(DISTINCT strain)
          FROM product_template
         WHERE strain IS NOT NULL AND strain <> ''
    """)
    populated, distinct = cr.fetchone()

    _logger.warning(
        "mint_api_v2 19.0.4.28.0 (phase 1): mint.strain installed, NOT seeded. "
        "%s templates carry strain text across %s distinct raw values. "
        "Run env['mint.strain'].seed_from_products() to build the master.",
        populated, distinct,
    )
