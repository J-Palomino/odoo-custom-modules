# -*- coding: utf-8 -*-
"""
Customer favorites — saved products and deals.

One row per (partner, item_type, item_ref). The reference keys are the
Dutchie-side identifiers the storefront already carries, verified against a
live inventory-service response 2026-08-24:

  product  ->  inventory row `product_id`   e.g. '13815543'   (string)
  deal     ->  discount row  `discount_id`  e.g. 385634       (int)

Both are stored as Char. `discount_id` is numeric today, but the column is a
reference key, not an arithmetic one, and keeping a single Char makes the
uniqueness constraint and the API payload uniform across both types.

Deliberately NOT keyed on `slug`: the same live probe returned `slug: None` on
a stocked product, so a slug-keyed favorite would be unsaveable for part of
the catalog.

`location_id` is context ("where they saw it"), NOT part of identity — a
customer who hearts the same product at two stores has one favorite, not two.

`label` / `image_url` are denormalized at capture time so the Favorites list
still renders after an item ages out of the inventory cache. Cached discounts
in particular are known to outlive their real Dutchie lifetime (retired
discounts never expire in the cache), so the list must never depend on a live
lookup succeeding.
"""

from odoo import models, fields


class MintCustomerFavorite(models.Model):
    _name = 'mint.customer.favorite'
    _description = 'Customer Favorite (product or deal)'
    _order = 'create_date desc, id desc'

    partner_id = fields.Many2one(
        'res.partner',
        string='Customer',
        required=True,
        index=True,
        ondelete='cascade',
    )

    item_type = fields.Selection(
        [
            ('product', 'Product'),
            ('deal', 'Deal'),
        ],
        string='Type',
        required=True,
        index=True,
    )

    item_ref = fields.Char(
        string='Reference',
        required=True,
        index=True,
        help="Dutchie product_id (products) or discount_id (deals), as text.",
    )

    location_id = fields.Char(
        string='Store (location UUID)',
        index=True,
        help='Inventory-service location UUID the item was favorited at. '
             'Context only — not part of the favorite identity.',
    )

    label = fields.Char(
        string='Label',
        help='Display name captured when favorited, so the list renders '
             'even if the item leaves the inventory cache.',
    )

    image_url = fields.Char(string='Image URL')

    _sql_constraints = [
        (
            'partner_item_uniq',
            'unique(partner_id, item_type, item_ref)',
            'This item is already in the customer\'s favorites.',
        ),
    ]
