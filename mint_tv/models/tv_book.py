from odoo import api, fields, models


class TvBook(models.Model):
    _name = 'mint.tv.book'
    _description = 'TV Book Viewer'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'sequence, id'

    name = fields.Char(string='Name', required=True, tracking=True,
                       help='e.g. Menu, Catalog, Welcome Guide')
    active = fields.Boolean(default=True)
    sequence = fields.Integer(default=10)
    company_id = fields.Many2one('res.company', string='Store',
                                 default=lambda self: self.env.company,
                                 tracking=True)

    auto_advance = fields.Integer(
        string='Auto-Advance (sec)', default=0,
        help='Seconds per page. 0 = manual swipe only.',
        tracking=True,
    )

    page_ids = fields.Many2many(
        'ir.attachment', 'tv_book_page_rel', 'book_id', 'attachment_id',
        string='Pages',
        domain="[('mimetype', 'in', ['image/png','image/jpeg','image/webp','image/gif','application/pdf'])]",
        help='Images and PDFs in page order.',
    )

    page_count = fields.Integer(string='Pages', compute='_compute_page_count')

    nodecg_url = fields.Char(
        string='NodeCG Base URL',
        compute='_compute_nodecg_url',
    )

    @api.depends('page_ids')
    def _compute_page_count(self):
        for rec in self:
            rec.page_count = len(rec.page_ids)

    def _compute_nodecg_url(self):
        base = self.env['ir.config_parameter'].sudo().get_param(
            'mint_tv.nodecg_url', 'https://tv.letsgomint.us')
        for rec in self:
            rec.nodecg_url = base

    def _to_api_dict(self):
        self.ensure_one()
        base = self.env['ir.config_parameter'].sudo().get_param(
            'web.base.url', 'https://letsgomint.us')
        return {
            'id': self.id,
            'name': self.name,
            'auto_advance': self.auto_advance,
            'company_id': self.company_id.id,
            'company_name': self.company_id.name,
            'pages': [{
                'id': att.id,
                'name': att.name,
                'mimetype': att.mimetype,
                'url': f'{base}/web/content/{att.id}/{att.name}',
            } for att in self.page_ids],
            'write_date': self.write_date,
        }
