from odoo import fields, models


class MintBrand(models.Model):
    _inherit = 'mint.brand'

    display_emoji = fields.Char(
        string='Display Emoji',
        size=8,
        help='Single emoji rendered before the brand name in budtender-facing '
             'PTL exports (Text Deals, SMS-friendly menu paste). Defaults to '
             '🌿 when empty. Keep to one character so monospace POS terminals '
             'render predictably.',
    )

    vendor_partner_ids = fields.Many2many(
        'res.partner',
        'mint_brand_vendor_partner_rel',
        'brand_id',
        'partner_id',
        string='Vendor Portal Contacts',
        help='Portal users (by partner) allowed to view this brand\'s promo '
             'calendar when logged in on the public /promos page.',
    )
