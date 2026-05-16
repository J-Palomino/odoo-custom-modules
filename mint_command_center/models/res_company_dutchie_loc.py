# -*- coding: utf-8 -*-
"""Dutchie POS LocId / LspId on res.company.

The Backoffice REST API is keyed by `LocId` (numeric per-store) and `LspId`
(numeric per-state market). Until now this mapping has only existed in
scripts (`packages/inventory-service/scripts/sync-all-markets.js` and
`scripts/_resync-state-deals.mjs` on the frontend repo). Without it on the
store record, `dutchie_discount_push._deal_to_dutchie_payload` falls back
to `int(store.dutchie_store_id)` — which is the consumer-side UUID, not a
numeric LocId — so `LocId` always resolved to 0 and Dutchie would reject.

Backfill of the 22 known mappings happens in 19.0.5.0.0/post-migrate.

NOTE: no `x_` prefix. The auto_init schema race (Odoo treats `x_*` fields as
runtime "studio" fields and creates them via a different schema-sync path
that runs in a phase that itself queries res.company) bit us with
`x_dutchie_discount_push_enabled`; same shape used here per
mint_command_center commit e0e789f and CLAUDE.md guidance.
"""
from odoo import fields, models


class ResCompany(models.Model):
    _inherit = 'res.company'

    dutchie_loc_id = fields.Integer(
        string='Dutchie POS LocId',
        help=(
            'Per-store Dutchie POS location ID. Used as the LocId in '
            'v2/discount/update-discount-item and similar Backoffice calls. '
            'Distinct from dutchie_store_id (the consumer/web UUID used '
            'by the storefront cache).'
        ),
        copy=False,
    )
    dutchie_lsp_id = fields.Integer(
        string='Dutchie LSP ID',
        help=(
            'Licensed Service Provider ID — Mint operates one LSP per market: '
            'AZ=575, MI=576, IL=805, NV=820, MO=723. Required alongside '
            'dutchie_loc_id for any Backoffice write. Could be derived from '
            'region_id, but storing it on the company record makes the '
            'payload builder a pure function of the store.'
        ),
        copy=False,
    )
