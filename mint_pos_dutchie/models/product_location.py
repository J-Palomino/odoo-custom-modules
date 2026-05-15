# -*- coding: utf-8 -*-
"""
Per-store product mapping — links shared product.template to Dutchie
identity at each store location.

Solves the ID mismatch: Odoo stores MongoDB ObjectIds (Plus API) as
dutchie_product_id on product.template, but Dutchie Backoffice POS API
uses numeric ProductIds that differ per location. This table provides
the per-store lookup the relay needs.

Also carries per-store pricing (for regional pricelists) and inventory
quantities (updated by the inventoryQuantitySync job).
"""
from odoo import api, fields, models


class MintProductLocation(models.Model):
    _name = 'mint.product.location'
    _description = 'Per-Store Product Mapping'
    _order = 'company_id, product_tmpl_id'

    product_tmpl_id = fields.Many2one(
        'product.template',
        string='Product',
        required=True,
        ondelete='cascade',
        index=True,
    )
    company_id = fields.Many2one(
        'res.company',
        string='Store',
        required=True,
        index=True,
    )

    # ── Dutchie identity (per-store) ─────────────────────────────────
    dutchie_product_id = fields.Char(
        string='Dutchie POS Product ID',
        index=True,
        help='Numeric ProductId from Dutchie Backoffice POS API. '
             'Differs per location for the same physical product.',
    )
    dutchie_plus_id = fields.Char(
        string='Dutchie Plus ID',
        index=True,
        help='MongoDB ObjectId from Dutchie Plus/Consumer API.',
    )
    dutchie_package_id = fields.Char(
        string='Dutchie Package ID',
        help='PackageId for inventory adjustments via Dutchie API.',
    )

    # ── Per-store pricing ────────────────────────────────────────────
    rec_price = fields.Float(
        string='Recreational Price',
        digits=(12, 2),
        help='Retail price at this store (from Dutchie RecUnitPrice).',
    )
    med_price = fields.Float(
        string='Medical Price',
        digits=(12, 2),
        help='Medical price at this store (if applicable).',
    )

    # ── Per-store inventory ──────────────────────────────────────────
    quantity_available = fields.Float(
        string='Qty Available',
        digits=(12, 2),
        help='Current stock at this store (synced from Dutchie).',
    )
    last_synced_at = fields.Datetime(
        string='Qty Last Synced',
        help='When quantity_available was last updated from Dutchie.',
    )

    # ── Barcode (from Dutchie ProductNo) ─────────────────────────────
    barcode = fields.Char(
        string='Barcode / SKU',
        index=True,
        help='ProductNo from Dutchie. Used for Zebra scanner matching in POS.',
    )

    # ── Status ───────────────────────────────────────────────────────
    active = fields.Boolean(
        string='Active',
        default=True,
        help='Whether this product is currently in this store inventory.',
    )
    synced_at = fields.Datetime(
        string='Last Full Sync',
        help='When this mapping was last synced from Dutchie.',
    )

    _product_company_uniq = models.Constraint(
        'UNIQUE(product_tmpl_id, company_id)',
        'Each product can only have one mapping per store.',
    )

    _dutchie_pid_company_uniq = models.Constraint(
        'UNIQUE(dutchie_product_id, company_id)',
        'Dutchie Product ID must be unique per store.',
    )
