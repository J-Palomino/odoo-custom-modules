# -*- coding: utf-8 -*-
"""Dutchie POS LocId/LspId on res.company.

The Dutchie Backoffice REST API is keyed by `LocId` (numeric per-store) and
`LspId` (numeric per-state/market). Until now this mapping has only existed
in scripts (`packages/inventory-service/scripts/sync-all-markets.js` and
`scripts/_resync-state-deals.mjs` on the frontend repo). For Odoo to publish
discounts directly to Dutchie POS we need the mapping on the store record.

Backfill of the 25 known mappings happens in the post-migrate script for
this module version.
"""
from odoo import fields, models


class ResCompany(models.Model):
    _inherit = 'res.company'

    x_dutchie_loc_id = fields.Integer(
        string='Dutchie POS LocId',
        help=(
            'Per-store Dutchie POS location ID. Used as the LocId in '
            'v2/discount/update-discount-item and similar Backoffice calls. '
            'Distinct from x_dutchie_store_id (the consumer/web UUID used '
            'by the storefront cache).'
        ),
        copy=False,
    )
    x_dutchie_lsp_id = fields.Integer(
        string='Dutchie LSP ID',
        help=(
            'Licensed Service Provider ID — Mint operates one LSP per market: '
            'AZ=575, MI=576, IL=805, NV=820, MO=723. Required alongside '
            'x_dutchie_loc_id for any Backoffice write.'
        ),
        copy=False,
    )
