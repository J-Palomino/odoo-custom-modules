import mimetypes
import uuid

from odoo import api, fields, models


class MintFlipbook(models.Model):
    _name = 'mint.flipbook'
    _description = 'Marketing Flipbook'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'create_date desc'

    name = fields.Char(string='Title', required=True, tracking=True)
    vendor_id = fields.Many2one(
        'res.partner', string='Vendor',
        help='Optional — the vendor whose offerings this flipbook presents.',
        tracking=True)
    description = fields.Text(string='Internal Notes')
    state = fields.Selection(
        [('draft', 'Draft'), ('published', 'Published')],
        string='Status', default='draft', required=True, tracking=True)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one(
        'res.company', string='Company',
        default=lambda self: self.env.company)

    page_ids = fields.One2many(
        'mint.flipbook.page', 'flipbook_id', string='Pages')
    page_count = fields.Integer(
        string='Pages', compute='_compute_page_count', store=True)

    # Generated artifacts — populated in later phases (PDF merge + viewer).
    pdf_file = fields.Binary(string='Flipbook PDF', attachment=True, readonly=True)
    pdf_filename = fields.Char(string='PDF Filename')
    access_token = fields.Char(
        string='Access Token', copy=False, readonly=True,
        default=lambda self: uuid.uuid4().hex)

    @api.depends('page_ids')
    def _compute_page_count(self):
        for rec in self:
            rec.page_count = len(rec.page_ids)

    def action_publish(self):
        self.write({'state': 'published'})

    def action_reset_draft(self):
        self.write({'state': 'draft'})


class MintFlipbookPage(models.Model):
    _name = 'mint.flipbook.page'
    _description = 'Flipbook Page'
    _order = 'sequence, id'

    flipbook_id = fields.Many2one(
        'mint.flipbook', string='Flipbook', required=True, ondelete='cascade')
    sequence = fields.Integer(default=10)
    name = fields.Char(string='Title')
    file = fields.Binary(string='File', required=True, attachment=True)
    filename = fields.Char(string='Filename')
    mimetype = fields.Char(
        string='Type', compute='_compute_mimetype', store=True)

    @api.depends('filename')
    def _compute_mimetype(self):
        for rec in self:
            rec.mimetype = mimetypes.guess_type(rec.filename or '')[0] or ''
