"""
Post-migration for 19.0.6.0.0 — conservative backfill of
product.template.x_product_line (the master "product line" used by the PTL
format rollup, mint.ptl.deal.format_key — phases A1/A2).

Why a whitelist instead of a blind name-parse
----------------------------------------------
Product names follow ``{Brand} - {Product Line} - {Strain} (...) (...)``, so the
product line is the 2nd `` - ``-delimited segment. A live audit (2026-05-28,
6000 most-recent products) showed that blind 2nd-segment parsing is ~97%
"fillable" but DANGEROUSLY noisy: alongside real lines ("Flower", "AIO
Distillate", "Gummies") it also lifts strain names ("Green Crack", "Hazmat OG",
"Gelato"), brand names ("Common Citizen", "Six Labs") and junk ("Sample
Employee"). Writing those into x_product_line would pollute format_key with
strains — the exact opposite of the rollup's goal (ticket #93688).

So this migration only backfills rows whose 2nd segment EXACTLY matches a vetted
vocabulary of product-form/line terms (PRODUCT_LINE_WHITELIST below). Everything
else is left blank — which is safe, because the storefront uses a
``format_key || name`` fallback, so an un-mastered product simply keeps today's
per-deal display. The long tail is curated by marketing via the review wizard
(follow-up subtask of #93687).

master_category is NOT used as a source: only 11 of 62,256 product.template rows
have it set, so it carries no signal.

Idempotent: only writes where x_product_line IS NULL, so re-running (or running
after marketing has curated some rows) never clobbers existing values.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)

# Vetted, unambiguous product-line / product-form terms. EXACT match only
# (parenthetical weight/type suffix stripped first). Curated from the live
# frequency audit; deliberately excludes anything that reads as a strain,
# brand, or sample/junk row. Marketing extends the long tail via the wizard.
PRODUCT_LINE_WHITELIST = [
    # Flower
    'Flower', 'Popcorn Flower', 'Shake', 'Bulk Deli Flower', 'Bulk Deli',
    # Pre-rolls
    'Preroll', 'Prerolls', 'Preroll 5pk', 'Prerolls 5pk',
    'Infused Preroll', 'Infused Preroll 5pk', 'Infused Honey Preroll',
    'Mini Prerolls 10pk', 'Diamond Infused Mini Prerolls 10pk',
    'Diamond Infused Preroll', 'Kief Infused Prerolls 6pk',
    'Long Lifts Prerolls', 'Ice Hash Rosin Infused Preroll',
    # Vapes
    'Disposable', 'Cartridge', 'AIO Distillate', 'Distillate AIO',
    'AIO Live Resin', 'Live Resin Disposable', 'Live Resin Cartridge',
    'Liquid Diamonds Disposable', 'Liquid Live Resin Cartridge',
    'Cartridge Distillate', 'AIO Live Reserve Line',
    # Concentrates
    'Budder 1g', 'Cured Badder', 'Live Badder', 'Live Resin Badder',
    'Live Resin Budder', 'Shatter', 'Crumble', 'Live Rosin', 'Live Hash Rosin',
    # Edibles
    'Gummies', 'Fruit Chews', 'Pearl Gummies', 'Syrup 1:1',
    # Beverages
    'Beverage', 'Beverages', 'Beverage Shot', 'Beverage Shots',
]


def migrate(cr, version):
    if not version:
        # Fresh install — nothing to back-fill.
        return

    _logger.info(
        "mint_command_center 19.0.6.0.0: back-filling product.template."
        "x_product_line from vetted product-line vocabulary"
    )

    # 2nd `` - `` segment, parenthetical suffix stripped, trimmed; only rows
    # with >= 3 segments (Brand - Line - Strain...) and an exact whitelist hit.
    cr.execute(
        r"""
        UPDATE product_template pt
           SET x_product_line = trimmed.line
          FROM (
              SELECT id,
                     btrim(
                         regexp_replace(
                             split_part(name, ' - ', 2),
                             '\s*\([^)]*\)\s*$', ''
                         )
                     ) AS line
                FROM product_template
               WHERE name IS NOT NULL
                 AND array_length(string_to_array(name, ' - '), 1) >= 3
                 AND (x_product_line IS NULL OR x_product_line = '')
          ) trimmed
         WHERE pt.id = trimmed.id
           AND trimmed.line = ANY(%s)
        """,
        (PRODUCT_LINE_WHITELIST,),
    )
    filled = cr.rowcount
    _logger.info(
        "mint_command_center 19.0.6.0.0: set x_product_line on %s product(s)",
        filled,
    )

    # format_key is intentionally NOT recomputed here. Computing it for all
    # ~7.6k deals means a product.template.search per deal (over ~62k products),
    # which cannot finish inside an HTTP-bound module upgrade — it rolled the
    # upgrade back repeatedly on prod (2026-05-29). The columns are pre-created
    # in pre-migrate.py so the ORM does not mass-recompute on load, and the
    # stored format_key is filled in batches afterward by the cron
    # ir_cron_fill_format_key (mint.ptl.deal._cron_fill_format_key). Until a
    # deal's key is filled the storefront uses its format_key||name fallback, so
    # behavior is unchanged meanwhile.
    _logger.info(
        "mint_command_center 19.0.6.0.0: x_product_line backfilled; format_key "
        "compute deferred to batch cron ir_cron_fill_format_key"
    )
