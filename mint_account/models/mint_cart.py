# -*- coding: utf-8 -*-
"""
mint.cart — per-partner, per-store persistent cart.

One row per (partner_id, company_id). Items stored as JSON to mirror the
frontend cart shape (productId, quantity, unitPrice, discountedPrice,
productName, brandName, imageUrl, sku, category, dealType, maxQuantity).

Kept as JSON (not O2M mint.cart.line records) for three reasons:
  1. The FE cart mutates constantly; creating/unlinking lines per mutation
     thrashes the DB with no upside.
  2. Product references in the cart point at Dutchie WhseProducts IDs, not
     Odoo product.product records — a FK wouldn't resolve anyway.
  3. A single round-trip to read/write the whole cart is simpler than an
     O2M bag, and cart size is tiny (≤ 20 items).
"""
from odoo import models, fields


class MintCart(models.Model):
    _name = 'mint.cart'
    _description = 'MintDeals persistent cart'
    _rec_name = 'display_name'

    partner_id = fields.Many2one(
        'res.partner', required=True, ondelete='cascade', index=True,
    )
    company_id = fields.Many2one(
        'res.company', required=True, ondelete='cascade', index=True,
    )
    items = fields.Text(
        default='[]',
        help="JSON array of cart items mirroring the FE cart shape.",
    )
    updated_at = fields.Datetime(default=fields.Datetime.now)
    display_name = fields.Char(compute='_compute_display_name')

    _sql_constraints = [
        (
            'partner_company_uniq',
            'unique(partner_id, company_id)',
            'A partner already has a cart for this store.',
        ),
    ]

    def _compute_display_name(self):
        for rec in self:
            rec.display_name = f"Cart: {rec.partner_id.display_name} @ {rec.company_id.name}"
