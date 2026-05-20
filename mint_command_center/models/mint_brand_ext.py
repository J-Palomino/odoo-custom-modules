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
