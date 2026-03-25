from odoo import api, fields, models


class TvLayout(models.Model):
    _name = 'mint.tv.layout'
    _description = 'TV Display Layout'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'sequence, id'

    name = fields.Char(string='Name', required=True, tracking=True,
                       help='e.g. Lobby, Bar Area, VIP Room')
    active = fields.Boolean(default=True)
    sequence = fields.Integer(default=10)
    company_id = fields.Many2one('res.company', string='Store',
                                 default=lambda self: self.env.company,
                                 tracking=True)

    cols = fields.Integer(string='Columns', default=2, required=True, tracking=True)
    rows = fields.Integer(string='Rows', default=2, required=True, tracking=True)
    cell_count = fields.Integer(string='Cells', compute='_compute_cell_count', store=True)

    slideshow_interval = fields.Integer(
        string='Slideshow Interval (sec)', default=0,
        help='Seconds between slides. 0 = disabled, shows first media only.',
        tracking=True,
    )

    media_ids = fields.Many2many(
        'ir.attachment', 'tv_layout_media_rel', 'layout_id', 'attachment_id',
        string='Media',
        domain="[('mimetype', 'in', ['image/png','image/jpeg','image/webp','image/gif','video/mp4','video/webm','video/quicktime'])]",
        help='Images and videos for this display. Order determines slideshow sequence.',
    )

    nodecg_url = fields.Char(
        string='NodeCG Base URL',
        compute='_compute_nodecg_url',
    )

    @api.depends('cols', 'rows')
    def _compute_cell_count(self):
        for rec in self:
            rec.cell_count = rec.cols * rec.rows

    @api.constrains('cols', 'rows')
    def _check_grid(self):
        for rec in self:
            if not (1 <= rec.cols <= 8 and 1 <= rec.rows <= 8):
                raise models.ValidationError('Cols and rows must be between 1 and 8.')
            if rec.cols * rec.rows > 16:
                raise models.ValidationError('Max 16 cells (cols x rows).')

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
            'cols': self.cols,
            'rows': self.rows,
            'slideshow_interval': self.slideshow_interval,
            'company_id': self.company_id.id,
            'company_name': self.company_id.name,
            'media': [{
                'id': att.id,
                'name': att.name,
                'mimetype': att.mimetype,
                'url': f'{base}/web/content/{att.id}/{att.name}',
            } for att in self.media_ids],
            'write_date': self.write_date,
        }
