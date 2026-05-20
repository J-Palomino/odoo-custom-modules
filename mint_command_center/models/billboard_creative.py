from odoo import fields, models


class MintBillboardCreative(models.Model):
    """A billboard creative asset — the artwork that runs on one or more boards.
    `drive_url` mirrors the React app's `google_drive_url`. Optional Odoo
    attachment for storing the artwork in-place."""

    _name = 'mint.billboard.creative'
    _description = 'Billboard Creative'
    _inherit = ['mail.thread']
    _order = 'state, name'

    name = fields.Char(string='Name', required=True, tracking=True)
    market_id = fields.Many2one('mint.region', string='Market', index=True)
    drive_url = fields.Char(string='Google Drive URL')
    size = fields.Char(string='Size')
    state = fields.Selection(
        selection=[
            ('active', 'Active'),
            ('archived', 'Archived'),
        ],
        string='Status',
        default='active',
        required=True,
        tracking=True,
    )
    attachment_id = fields.Many2one(
        'ir.attachment',
        string='Artwork',
        domain="[('res_model', '=', 'mint.billboard.creative')]",
    )
    notes = fields.Text(string='Notes')
