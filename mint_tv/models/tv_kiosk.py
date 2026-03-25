from odoo import api, fields, models


class TvKiosk(models.Model):
    _name = 'mint.tv.kiosk'
    _description = 'TV Kiosk Browser'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'sequence, id'

    name = fields.Char(string='Name', required=True, tracking=True,
                       help='e.g. Deals Kiosk, Store Menu')
    active = fields.Boolean(default=True)
    sequence = fields.Integer(default=10)
    company_id = fields.Many2one('res.company', string='Store',
                                 default=lambda self: self.env.company,
                                 tracking=True)

    url = fields.Char(string='URL', required=True,
                      default='https://mintdeals2026.pages.dev',
                      help='Website to display in the kiosk frame.',
                      tracking=True)

    allow_interaction = fields.Boolean(
        string='Allow Touch/Click', default=True,
        help='If enabled, users can interact with the page. If disabled, it is view-only.',
    )

    refresh_interval = fields.Integer(
        string='Auto-Refresh (sec)', default=0,
        help='Reload the page every N seconds. 0 = never.',
        tracking=True,
    )

    nodecg_url = fields.Char(
        string='NodeCG Base URL',
        compute='_compute_nodecg_url',
    )

    def _compute_nodecg_url(self):
        base = self.env['ir.config_parameter'].sudo().get_param(
            'mint_tv.nodecg_url', 'https://tv.letsgomint.us')
        for rec in self:
            rec.nodecg_url = base

    def _to_api_dict(self):
        self.ensure_one()
        return {
            'id': self.id,
            'name': self.name,
            'url': self.url,
            'allow_interaction': self.allow_interaction,
            'refresh_interval': self.refresh_interval,
            'company_id': self.company_id.id,
            'company_name': self.company_id.name,
            'write_date': self.write_date,
        }
