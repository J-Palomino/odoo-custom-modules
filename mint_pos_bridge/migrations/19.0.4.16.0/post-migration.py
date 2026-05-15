# -*- coding: utf-8 -*-
"""
Post-migration for mint_pos_bridge 19.0.4.16.0.

Background:
  19.0.4.15.0 shipped a `data/dutchie_product_categories.xml` that created a
  parallel `Dutchie/<MasterCategory>/<ProductCategory>` tree under product.category
  with 83 records, plus a fresh `dutchie_category_id` (Integer) declaration.

  That decision was wrong:
    1. The production tree is `Cannabis/...` (51K+ products under it).
    2. The new tree had 0 products and added duplicate-key lookups for
       `dutchie_category_id` (already-existing 52-of-73 dup issue, made worse).
    3. The field name collided with `mint_api_v2`'s `fields.Char` declaration.

19.0.4.16.0 rolls back:
  - Field type reverted to Char in models/product_category.py (matches mint_api_v2).
  - data/dutchie_product_categories.xml deleted from manifest + disk.
  - This migration deletes the 83 records the prior version created (by xmlid),
    then backfills `dutchie_master_category` on existing categories whose
    `dutchie_category_id` is recognized in the Dutchie catalog.

Idempotent: running multiple times has no further effect once tree is removed
and master_category is set.
"""

import logging

_logger = logging.getLogger(__name__)


# 72 ProductCategoryId → MasterCategory mappings.
# Generated from scripts/.product-categories.json (Dutchie product-master/get-product-categories
# at LSP 575, fetched 2026-05-11). Regenerate via scripts/fetch-product-categories.mjs
# in mintdeals2025__FrontEnd repo if Dutchie evolves.
DUTCHIE_CATEGORY_TO_MASTER = {
    '22848': 'Flower', '22968': 'Edible', '23156': 'Flower', '23158': 'Edible',
    '23371': 'Concentrate', '23373': 'Bulk', '23377': 'Edible', '23378': 'Concentrate',
    '23436': 'Unmedicated', '23440': 'Edible', '23442': 'Unmedicated', '23443': 'Vape',
    '23444': 'Concentrate', '23448': 'Edible', '23449': 'Bulk', '23452': 'Medicated',
    '23453': 'Medicated', '23454': 'Vape', '23464': 'Edible', '23465': 'Unmedicated',
    '23466': 'Concentrate', '23470': 'Medicated', '23669': 'Bulk', '23681': 'Flower',
    '23682': 'Concentrate', '23683': 'Unmedicated', '23823': 'Vape', '23824': 'Vape',
    '23880': 'Bulk', '23881': 'Bulk', '24132': 'Concentrate', '25808': 'Waste',
    '26271': 'Bulk', '34559': 'Flower', '35492': 'Concentrate', '39579': 'Bulk',
    '39626': 'Cultivation', '39873': 'Bulk', '39942': 'Flower', '39943': 'Flower',
    '39944': 'Medicated', '39946': 'Bulk', '40023': 'Flower', '40110': 'Flower',
    '43966': 'Cultivation', '43990': 'Flower', '44349': 'Unmedicated', '44350': 'Unmedicated',
    '44356': 'Vape', '44357': 'Vape', '44358': 'Cultivation', '44359': 'Medicated',
    '44558': 'Cultivation', '44602': 'Concentrate', '44603': 'Flower', '44604': 'Edible',
    '44605': 'Vape', '44606': 'Medicated', '44653': 'Vape', '45354': 'Unmedicated',
    '45355': 'Unmedicated', '45531': 'Vape', '45665': 'Flower', '45667': 'Ingredients/Supplies',
    '45668': 'Ingredients/Supplies', '45669': 'Ingredients/Supplies', '45670': 'Ingredients/Supplies',
    '45671': 'Ingredients/Supplies', '45683': 'Unmedicated', '45692': 'Ingredients/Supplies',
    '45693': 'Ingredients/Supplies', '45695': 'Ingredients/Supplies',
}


def migrate(cr, version):
    _logger.info(
        'mint_pos_bridge 19.0.4.16.0 post-migration: rolling back parallel Dutchie/ tree, '
        'enriching Cannabis/ tree with dutchie_master_category'
    )

    # --- 1. Delete the 83 records created by the prior version's XML data file. ---
    # ir.model.data records have module='mint_pos_bridge' and a name starting with
    # 'dutchie_category_'. Resolve their product.category res_ids and delete in two steps.
    cr.execute(
        """
        SELECT res_id FROM ir_model_data
        WHERE module = 'mint_pos_bridge'
          AND model = 'product.category'
          AND name LIKE 'dutchie_category_%'
        """
    )
    orphan_ids = [row[0] for row in cr.fetchall()]
    _logger.info('  found %d orphan product.category records from 19.0.4.15.0 to remove', len(orphan_ids))

    if orphan_ids:
        # First clear any product.template still pointing at these (paranoia — shouldn't be any).
        cr.execute('SELECT COUNT(*) FROM product_template WHERE categ_id IN %s', (tuple(orphan_ids),))
        in_use = cr.fetchone()[0]
        if in_use:
            _logger.warning(
                '  %d product.template rows still point at orphan Dutchie/ categories — '
                'leaving them in place (will be fixed manually).', in_use
            )
        else:
            # Safe to delete — order matters: leaves before parents, so order by parent_id DESC.
            cr.execute(
                'DELETE FROM product_category WHERE id IN %s',
                (tuple(orphan_ids),),
            )
            cr.execute(
                "DELETE FROM ir_model_data "
                "WHERE module = 'mint_pos_bridge' "
                "  AND model = 'product.category' "
                "  AND name LIKE 'dutchie_category_%'"
            )
            _logger.info('  removed %d product.category records + ir_model_data refs', len(orphan_ids))

    # --- 2. Backfill dutchie_master_category on the surviving Cannabis/... tree. ---
    # Only update rows that have dutchie_category_id set AND whose value is a known
    # Dutchie ProductCategoryId. Skip rows where master_category is already set (idempotent).
    updated = 0
    for cat_id, master in DUTCHIE_CATEGORY_TO_MASTER.items():
        cr.execute(
            """
            UPDATE product_category
               SET dutchie_master_category = %s
             WHERE dutchie_category_id = %s
               AND (dutchie_master_category IS NULL OR dutchie_master_category = '')
            """,
            (master, cat_id),
        )
        updated += cr.rowcount
    _logger.info('  enriched %d existing product.category rows with dutchie_master_category', updated)

    _logger.info('mint_pos_bridge 19.0.4.16.0 post-migration complete')
