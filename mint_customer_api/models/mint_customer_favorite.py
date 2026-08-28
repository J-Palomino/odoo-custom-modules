# -*- coding: utf-8 -*-
"""
Customer favorites — saved products and deals.

One row per (partner, item_type, item_ref). The reference keys are the
Dutchie-side identifiers the storefront already carries, verified against a
live inventory-service response 2026-08-24:

  product  ->  inventory row `product_id`   e.g. '13815543'   (string)
  deal     ->  discount row  `discount_id`  e.g. 385634       (int)
  store    ->  res.company id              e.g. 18           (int)

Stores key on the Odoo company id rather than the slug: the id is what an
order already carries (`company_id`), and slugs are editable in Odoo — a
rename would silently orphan every saved store. It is deliberately NOT the
Dutchie location UUID, which belongs to the inventory cache and is absent
for pre-launch stores that a customer may still want to follow.

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
            ('store', 'Store'),
        ],
        string='Type',
        required=True,
        index=True,
    )

    item_ref = fields.Char(
        string='Reference',
        required=True,
        index=True,
        help="Dutchie product_id (products), discount_id (deals), or the "
             "res.company id (stores), as text.",
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

    # Odoo 19 declares SQL constraints as models.Constraint attributes. The
    # older `_sql_constraints = [(name, definition, message)]` list form is
    # accepted without error but SILENTLY NEVER APPLIED on 19 — verified on
    # staging 2026-08-24, where every models.Constraint declaration in the
    # addons path had produced a real constraint while every _sql_constraints
    # one (mint_cart.partner_company_uniq, mint_discount.redemption_code_unique,
    # and this model's first cut) had produced none.
    #
    # This uniqueness is load-bearing: the REST endpoint's idempotency relies
    # on at most one row per (partner, type, ref), so a silently-absent
    # constraint would let concurrent double-taps insert duplicates.
    _partner_item_uniq = models.Constraint(
        'UNIQUE(partner_id, item_type, item_ref)',
        "This item is already in the customer's favorites.",
    )
