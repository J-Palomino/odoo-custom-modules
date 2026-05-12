# -*- coding: utf-8 -*-
from odoo import fields, models


class ProductCategory(models.Model):
    _inherit = 'product.category'

    # NOTE: mint_api_v2 already declares dutchie_category_id as fields.Char on
    # product.category. We re-declare (idempotently) with the same type so this
    # module can ship even when mint_api_v2 isn't loaded, AND so the upgrade
    # path doesn't flip the column type. Do NOT change to Integer — existing
    # data is string-typed and 50K+ products reference these categories.
    dutchie_category_id = fields.Char(
        string='Dutchie ProductCategoryId',
        index=True,
        help='ProductCategoryId from Dutchie Backoffice (LSP 575). '
             'Authoritative key for product create/update via update-product.',
    )
    dutchie_master_category = fields.Selection(
        selection=[
            ('Bulk', 'Bulk'),
            ('Concentrate', 'Concentrate'),
            ('Cultivation', 'Cultivation'),
            ('Edible', 'Edible'),
            ('Flower', 'Flower'),
            ('Ingredients/Supplies', 'Ingredients/Supplies'),
            ('Medicated', 'Medicated'),
            ('Unmedicated', 'Unmedicated'),
            ('Vape', 'Vape'),
            ('Waste', 'Waste'),
        ],
        string='Dutchie MasterCategory',
        help='Grouping bucket from Dutchie. The MasterCategory string on a product '
             'record is ignored on create — ProductCategoryId is authoritative.',
    )
