# -*- coding: utf-8 -*-
from odoo import fields, models


class ProductCategory(models.Model):
    _inherit = 'product.category'

    dutchie_category_id = fields.Integer(
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
