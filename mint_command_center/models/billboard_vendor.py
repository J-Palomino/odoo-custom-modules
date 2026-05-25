from odoo import api, fields, models


def _sync_partner_ad_tagging(billboard_vendors):
    """Set `x_is_ad_vendor=True, x_ad_vendor_type='billboard'` on the
    partner backing each billboard vendor. Idempotent. Called whenever a
    `partner_id` is set or changed."""
    partners = billboard_vendors.mapped('partner_id').filtered(lambda p: p)
    for p in partners:
        vals = {}
        if not p.x_is_ad_vendor:
            vals['x_is_ad_vendor'] = True
        if not p.x_ad_vendor_type:
            vals['x_ad_vendor_type'] = 'billboard'
        if vals:
            p.write(vals)


class MintBillboardVendor(models.Model):
    """Billboard vendor — Lamar, Outfront, etc. Owns one or more boards.
    Contacts live on `res.partner` via `partner_id`/`partner_id.child_ids` so
    they participate in the rest of Odoo's contact/mail tooling natively."""

    _name = 'mint.billboard.vendor'
    _description = 'Billboard Vendor'
    _inherit = ['mail.thread']
    _order = 'name'

    name = fields.Char(string='Name', required=True, tracking=True)
    partner_id = fields.Many2one(
        'res.partner',
        string='Billing Partner',
        help='res.partner backing this vendor — used for contacts (child_ids), '
             'invoices, and any cross-Odoo references.',
        ondelete='restrict',
    )
    market_ids = fields.Many2many(
        'mint.region',
        string='Markets',
        help='Markets this vendor operates in.',
    )
    notes = fields.Text(string='Notes')
    active = fields.Boolean(string='Active', default=True)

    contact_ids = fields.One2many(
        related='partner_id.child_ids',
        string='Contacts',
        readonly=False,
    )
    board_ids = fields.One2many(
        'mint.billboard.board',
        'vendor_id',
        string='Boards',
    )
    board_count = fields.Integer(
        string='Boards',
        compute='_compute_counts',
    )

    @api.depends('board_ids')
    def _compute_counts(self):
        for rec in self:
            rec.board_count = len(rec.board_ids)

    @api.model_create_multi
    def create(self, vals_list):
        recs = super().create(vals_list)
        _sync_partner_ad_tagging(recs)
        return recs

    def write(self, vals):
        result = super().write(vals)
        if 'partner_id' in vals:
            _sync_partner_ad_tagging(self)
        return result

    def action_view_boards(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': f'Boards — {self.name}',
            'res_model': 'mint.billboard.board',
            'view_mode': 'list,form',
            'domain': [('vendor_id', '=', self.id)],
            'context': {'default_vendor_id': self.id},
        }
