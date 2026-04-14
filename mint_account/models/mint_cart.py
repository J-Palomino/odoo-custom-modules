# -*- coding: utf-8 -*-
"""
mint.cart — per-partner, per-store persistent cart.

One row per (partner_id, company_id). Items stored as JSON to mirror the
frontend cart shape:
    [
        {
            "productId": "13798777",
            "inventoryId": "abc123",
            "quantity": 2,
            "unitPrice": 25.0,
            "discountedPrice": 22.5,
            "productName": "Biker Kush 14g",
            "brandName": "Indica Cannabis Co",
            "imageUrl": "...",
            "sku": "LPR-BKR-14G",
            "category": "flower",
            "dealType": null,
            "maxQuantity": 10
        },
        ...
    ]

Why JSON (not O2M mint.cart.line records):
  1. Items in the cart reference Dutchie WhseProducts IDs, not Odoo
     product.product records — an O2M FK wouldn't resolve anyway.
  2. FE mutates cart constantly; per-mutation create/unlink thrashes the
     DB with no upside.
  3. Cart is tiny (≤ 20 items typical) and always round-tripped whole;
     a single JSON read/write is simpler than an O2M bag.
"""
import json
import logging

from odoo import models, fields, api
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)


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

    # ── Compute ────────────────────────────────────────────────────────────

    def _compute_display_name(self):
        for rec in self:
            rec.display_name = (
                f"Cart: {rec.partner_id.display_name} @ {rec.company_id.name}"
            )

    # ── Validation ─────────────────────────────────────────────────────────

    @api.constrains('items')
    def _check_items_shape(self):
        """Reject items that aren't a JSON array. Individual item shape is
        validated by the controller (it has the FE payload context), not here."""
        for rec in self:
            try:
                parsed = json.loads(rec.items or '[]')
            except (ValueError, TypeError) as e:
                raise ValidationError(f"mint.cart.items must be valid JSON: {e}")
            if not isinstance(parsed, list):
                raise ValidationError("mint.cart.items must be a JSON array")

    # ── Helpers ────────────────────────────────────────────────────────────

    def items_list(self):
        """Parse items JSON into a list. Returns [] on any error."""
        self.ensure_one()
        try:
            return json.loads(self.items or '[]')
        except (ValueError, TypeError):
            return []

    def set_items(self, items):
        """Overwrite cart items. `items` is a list of dicts from the FE."""
        self.ensure_one()
        self.write({
            'items': json.dumps(items, default=str),
            'updated_at': fields.Datetime.now(),
        })

    @api.model
    def get_or_create(self, partner_id, company_id):
        """Fetch the partner's cart for this company, creating an empty one
        if missing. Returns the record."""
        cart = self.search([
            ('partner_id', '=', partner_id),
            ('company_id', '=', company_id),
        ], limit=1)
        if not cart:
            cart = self.create({
                'partner_id': partner_id,
                'company_id': company_id,
                'items': '[]',
            })
        return cart

    def merge_items(self, incoming):
        """Union incoming items with current cart items.

        Dedup rule: items with the same productId (string-compared) combine
        by summing quantity; unitPrice/discountedPrice/metadata are taken
        from the most-recent version (incoming wins over existing). Quantity
        is capped at maxQuantity if the item defines one, else uncapped.

        Mutates in place. Returns the merged list."""
        self.ensure_one()
        existing = self.items_list()
        by_pid = {str(it.get('productId')): dict(it) for it in existing}
        for it in (incoming or []):
            pid = str(it.get('productId'))
            if not pid or pid == 'None':
                continue  # drop malformed
            if pid in by_pid:
                merged = dict(by_pid[pid])
                merged['quantity'] = (merged.get('quantity') or 0) + (it.get('quantity') or 0)
                # Metadata: take the fresher values from the incoming item
                for k, v in it.items():
                    if k != 'quantity' and v is not None:
                        merged[k] = v
                by_pid[pid] = merged
            else:
                by_pid[pid] = dict(it)
            # Cap quantity if the item declared a max
            mx = by_pid[pid].get('maxQuantity')
            if isinstance(mx, (int, float)) and mx > 0 and by_pid[pid]['quantity'] > mx:
                by_pid[pid]['quantity'] = mx
        merged_list = list(by_pid.values())
        self.set_items(merged_list)
        return merged_list
