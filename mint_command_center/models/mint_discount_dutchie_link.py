# -*- coding: utf-8 -*-
"""Per-store Dutchie publish link for mint.discount.

One Odoo `mint.discount` record can target N stores (m2m `store_ids`). Dutchie's
`update-discount-item` endpoint is keyed by `LocId`, so each (discount, store)
pair becomes one Dutchie discount record. This link table tracks the per-store
sync state and the Dutchie ID returned on create.
"""
from odoo import api, fields, models


class MintDiscountDutchieLink(models.Model):
    _name = 'mint.discount.dutchie.link'
    _description = 'Dutchie publish link (mint.discount × store)'
    _rec_name = 'display_name'
    _order = 'discount_id, location_id'

    discount_id = fields.Many2one(
        'mint.discount', string='Discount',
        required=True, ondelete='cascade', index=True,
    )
    location_id = fields.Many2one(
        'res.company', string='Store',
        required=True, ondelete='restrict', index=True,
    )

    # Cached at link-create time — avoids races if a company's LocId is
    # changed between record creation and publish.
    dutchie_loc_id = fields.Integer(string='Dutchie LocId', readonly=True)
    dutchie_lsp_id = fields.Integer(string='Dutchie LspId', readonly=True)

    # Returned by Dutchie on successful create — bare integer per
    # update-discount-item response shape (Data is a number, not Data.Id).
    dutchie_discount_id = fields.Char(string='Dutchie Discount ID', readonly=True)

    state = fields.Selection([
        ('pending', 'Pending'),
        ('synced', 'Synced'),
        ('failed', 'Failed'),
        ('skipped', 'Skipped'),
    ], string='State', default='pending', required=True, index=True)

    synced_at = fields.Datetime(string='Last Synced', readonly=True)
    last_error = fields.Text(string='Last Error', readonly=True)
    raw_request = fields.Text(string='Last Request Payload', readonly=True)
    raw_response = fields.Text(string='Last Response', readonly=True)
    attempts = fields.Integer(string='Publish Attempts', default=0, readonly=True)

    display_name = fields.Char(compute='_compute_display_name', store=False)

    _sql_constraints = [
        ('uniq_discount_location', 'unique(discount_id, location_id)',
         'Each (discount, store) pair has at most one Dutchie publish link.'),
    ]

    @api.depends('discount_id.name', 'location_id.name', 'state')
    def _compute_display_name(self):
        for rec in self:
            disc = rec.discount_id.name or '?'
            loc = rec.location_id.name or '?'
            rec.display_name = f'{disc} → {loc} [{rec.state}]'

    @api.model_create_multi
    def create(self, vals_list):
        # Snapshot the company's LocId/LspId at link creation so a later
        # company-side edit can't invalidate already-synced rows.
        for vals in vals_list:
            if 'dutchie_loc_id' not in vals or 'dutchie_lsp_id' not in vals:
                comp = self.env['res.company'].browse(vals.get('location_id'))
                if comp.exists():
                    vals.setdefault('dutchie_loc_id', comp.x_dutchie_loc_id or 0)
                    vals.setdefault('dutchie_lsp_id', comp.x_dutchie_lsp_id or 0)
        return super().create(vals_list)
