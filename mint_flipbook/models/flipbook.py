import base64
import io
import logging
import mimetypes
import uuid

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools.pdf import merge_pdf

_logger = logging.getLogger(__name__)


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

    # Generated merged PDF (Phase 2). access_token reserved for the Phase 3 viewer.
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
        return True

    def action_reset_draft(self):
        self.write({'state': 'draft'})
        return True

    def action_generate_pdf(self):
        """Merge the ordered pages into one downloadable PDF.

        PDF pages are used as-is; image pages (PNG/JPG/…) are converted to
        single-page PDFs first, then everything is stitched with
        ``odoo.tools.pdf.merge_pdf``.
        """
        self.ensure_one()
        if not self.page_ids:
            raise UserError(_("Add at least one page before generating the PDF."))
        pdf_blobs = []
        skipped = []
        for page in self.page_ids.sorted(lambda p: (p.sequence, p.id)):
            if not page.file:
                continue
            raw = base64.b64decode(page.file)
            mime = page.mimetype or mimetypes.guess_type(page.filename or '')[0] or ''
            if mime == 'application/pdf':
                pdf_blobs.append(raw)
            elif mime.startswith('image/'):
                pdf_blobs.append(self._image_to_pdf(raw))
            else:
                skipped.append(page.name or page.filename or _("untitled page"))
        if not pdf_blobs:
            raise UserError(_("No PDF or image pages to merge."))
        merged = merge_pdf(pdf_blobs)
        safe_name = (self.name or 'flipbook').strip().replace('/', '-')
        self.write({
            'pdf_file': base64.b64encode(merged),
            'pdf_filename': '%s.pdf' % safe_name,
        })
        msg = _("Generated flipbook PDF from %d page(s).") % len(pdf_blobs)
        if skipped:
            msg += _(" Skipped %(n)d unsupported page(s): %(names)s") % {
                'n': len(skipped), 'names': ", ".join(skipped)}
        self.message_post(body=msg)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'type': 'success',
                'title': _("Flipbook PDF"),
                'message': msg,
                'sticky': False,
            },
        }

    @staticmethod
    def _image_to_pdf(raw):
        """Convert raw image bytes to a single-page PDF (bytes)."""
        from PIL import Image
        img = Image.open(io.BytesIO(raw))
        if img.mode not in ('RGB', 'L'):
            img = img.convert('RGB')
        buf = io.BytesIO()
        img.save(buf, format='PDF')
        return buf.getvalue()


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
