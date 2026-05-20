from odoo import _, api, exceptions, fields, models


class ResPartner(models.Model):
    """Advertising-vendor discriminator on res.partner.

    Rather than build a parallel `mint.advertising.vendor` table, we tag the
    underlying `res.partner` row and reuse Odoo-native contacts (child_ids),
    email, phone, website. A billboard vendor's `partner_id` is auto-flagged
    `x_is_ad_vendor=True, x_ad_vendor_type='billboard'` (see `mint_billboard_vendor_partner_sync`)
    so the unified Ad Vendors menu shows every paid-media vendor in one list.
    """

    _inherit = 'res.partner'

    x_is_ad_vendor = fields.Boolean(
        string='Is Ad Vendor',
        index=True,
        help='Tagged as an advertising/media vendor — appears in Mint Marketing → Operations → Advertising Vendors.',
    )
    x_ad_vendor_type = fields.Selection(
        selection=[
            ('billboard', 'Billboard'),
            ('print', 'Print'),
            ('digital', 'Digital'),
            ('radio', 'Radio'),
            ('other', 'Other'),
        ],
        string='Ad Vendor Type',
    )
    x_ad_market_ids = fields.Many2many(
        'mint.region',
        'res_partner_mint_region_ad_rel',
        'partner_id',
        'region_id',
        string='Ad Markets',
    )
    x_ad_document_ids = fields.One2many(
        'mint.advertising.document',
        'partner_id',
        string='Documents',
    )
    x_is_primary_ad_contact = fields.Boolean(
        string='Primary Ad Contact',
        help='When set on a contact (a partner whose parent_id is an ad vendor), '
             'marks them as the primary point of contact for that vendor.',
    )

    @api.constrains('x_is_primary_ad_contact', 'parent_id')
    def _check_single_primary_ad_contact(self):
        """Only one primary ad contact per vendor partner."""
        for rec in self:
            if not rec.x_is_primary_ad_contact or not rec.parent_id:
                continue
            others = self.search([
                ('id', '!=', rec.id),
                ('parent_id', '=', rec.parent_id.id),
                ('x_is_primary_ad_contact', '=', True),
            ], limit=1)
            if others:
                raise exceptions.ValidationError(
                    _('%(vendor)s already has %(other)s marked as primary contact.') % {
                        'vendor': rec.parent_id.display_name,
                        'other': others.display_name,
                    }
                )
