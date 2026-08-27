"""
Post-migration for 19.0.4.28.0 — strain master schema only. Does NOT seed.

The seed (`env['mint.strain'].seed_from_products()`) creates ~4,172 records
and rewrites `strain_id` on ~41,338 product templates. That is a data
migration, and this environment has no usable rehearsal: the `staging` branch
is an abandoned line, 317 commits behind main, so an upgrade there proves
nothing about what the seed does to prod.

So the two are deliberately split. This upgrade ships the model, the column
and the UI — after which the Strain dropdown exists and is empty — and the
seed is run by hand against a live database whose before/after counts can be
read back and checked. Re-running the seed later is safe either way; it is
idempotent and folds new spellings into existing masters.

To seed, once this version is live:

    env['mint.strain'].seed_from_products()      # -> summary dict

It logs and returns
{raw_values, buckets, created, aliases_added, skipped_thin,
 placeholder_rows_skipped, products_linked}.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    cr.execute("SELECT COUNT(*) FROM product_template WHERE strain IS NOT NULL AND strain <> ''")
    populated = cr.fetchone()[0]
    cr.execute("""
        SELECT COUNT(DISTINCT strain) FROM product_template
         WHERE strain IS NOT NULL AND strain <> ''
    """)
    distinct = cr.fetchone()[0]

    _logger.warning(
        "mint_api_v2 19.0.4.28.0: strain master schema installed but NOT seeded. "
        "%s templates carry strain text across %s distinct raw values. "
        "Run env['mint.strain'].seed_from_products() to build the master and link them.",
        populated, distinct,
    )
