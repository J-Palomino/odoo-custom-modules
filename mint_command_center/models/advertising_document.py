from odoo import fields, models


class MintAdvertisingDocument(models.Model):
    """Document link attached to an advertising vendor partner — rate cards,
    media kits, contracts. Mirrors the React app's `document_links` JSON
    array on `advertising_vendors`."""

    _name = 'mint.advertising.document'
    _description = 'Ad Vendor Document Link'
    _order = 'partner_id, sequence, id'

    partner_id = fields.Many2one(
        'res.partner',
        string='Vendor',
        required=True,
        index=True,
        ondelete='cascade',
    )
    label = fields.Char(string='Label', required=True)
    url = fields.Char(string='URL', required=True)
    sequence = fields.Integer(default=10)
