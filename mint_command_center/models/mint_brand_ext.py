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
    brand_class = fields.Selection(
        selection=[
            ('house', 'House'),
            ('partner', 'Partner'),
            ('third_party', 'Third-Party'),
        ],
        string='Brand Class',
        default='third_party',
        required=True,
        index=True,
        help='Merchandising classifier. "House" marks Mint-owned brands so the '
             'daily-deals sheet pins them to the top of each category and stars '
             'them. "Partner" is for co-marketed vendors; everything else is '
             '"Third-Party".',
    )
