from odoo import fields, models


class MintBillboardBoard(models.Model):
    """A physical billboard owned by a vendor. `board_code` is the
    vendor-supplied panel ID (React app field `board_id`). `geopath_id`
    is the standards-body identifier when available."""

    _name = 'mint.billboard.board'
    _description = 'Billboard'
    _inherit = ['mail.thread']
    _order = 'vendor_id, market_id, board_code'

    vendor_id = fields.Many2one(
        'mint.billboard.vendor',
        string='Vendor',
        required=True,
        index=True,
        ondelete='cascade',
        tracking=True,
    )
    market_id = fields.Many2one(
        'mint.region',
        string='Market',
        index=True,
    )
    board_code = fields.Char(
        string='Board ID',
        required=True,
        help='Vendor-supplied panel identifier.',
        tracking=True,
    )
    geopath_id = fields.Char(string='Geopath ID')
    description = fields.Text(string='Description')
    size = fields.Char(string='Size')
    face_direction = fields.Char(string='Face Direction')
    nearest_company_id = fields.Many2one(
        'res.company',
        string='Nearest Mint Store',
        help='Closest Mint location — used for buying decisions.',
    )
    audience_type = fields.Char(string='Audience Type')
    notes = fields.Text(string='Notes')
    active = fields.Boolean(string='Active', default=True)

    _sql_constraints = [
        ('board_code_per_vendor_unique',
         'unique(vendor_id, board_code)',
         'Board IDs must be unique per vendor.'),
    ]
