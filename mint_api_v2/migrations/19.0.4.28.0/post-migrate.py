"""
Post-migration for 19.0.4.28.0 — seed mint.strain and link products to it.

Runs after the registry is loaded, so it can use the ORM. Seeds the strain
master from the 4,800 distinct raw values already in
`product.template.strain`, folding spelling variants ("BLUE DREAM",
"Blue Dream (S)") onto one record as aliases, then back-fills
`product.template.strain_id` in bulk SQL.

Idempotent — re-running folds new spellings into existing masters instead of
creating duplicates, so it is safe if the upgrade is retried.

Note: this runs on EVERY environment the module upgrades into, including
staging, and derives entirely from that database's own data. There is no
hardcoded strain list to drift.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    env = api.Environment(cr, SUPERUSER_ID, {})

    if 'mint.strain' not in env:
        _logger.error("mint_api_v2 19.0.4.28.0 post-migrate: mint.strain missing from registry; skipping seed")
        return

    summary = env['mint.strain'].seed_from_products()
    _logger.info(
        "mint_api_v2 19.0.4.28.0 post-migrate: seeded strain master — "
        "%(raw_values)s raw values -> %(buckets)s strains "
        "(%(created)s created, %(aliases_added)s aliases added, "
        "%(placeholder_rows_skipped)s placeholder rows skipped, "
        "%(products_linked)s products linked)",
        summary,
    )
