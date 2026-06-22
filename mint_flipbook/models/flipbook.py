import base64
import io
import logging
import mimetypes
import uuid

from markupsafe import escape

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools.pdf import merge_pdf

_logger = logging.getLogger(__name__)

# Email-safe (table layout + inline styles, no JS/iframe) building blocks for
# the "embed in an HTML email" feature. Tokens (__NAME__/__SRC__/__ALT__/__URL__
# /__ROWS__) are substituted with str.replace so CSS "100%" and "{}" never get
# mangled by %-/format()-style interpolation.
_EMAIL_IMG_ROW = (
    '      <tr><td align="center" style="padding:0 0 6px 0;">'
    '<a href="__URL__" target="_blank" style="text-decoration:none;">'
    '<img src="__SRC__" alt="__ALT__" width="600" '
    'style="display:block;width:100%;max-width:600px;height:auto;border:0;'
    'outline:none;text-decoration:none;" /></a></td></tr>'
)

_EMAIL_TEMPLATE = (
    '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
    'border="0" style="border-collapse:collapse;background:#f4f4f4;">\n'
    '  <tr><td align="center" style="padding:16px;">\n'
    '    <table role="presentation" width="600" cellpadding="0" cellspacing="0" '
    'border="0" style="width:600px;max-width:600px;border-collapse:collapse;'
    'font-family:Arial,Helvetica,sans-serif;">\n'
    '      <tr><td align="center" style="padding:0 0 12px 0;font-size:20px;'
    'line-height:26px;font-weight:bold;color:#1a1a1a;">__NAME__</td></tr>\n'
    '__ROWS__\n'
    '      <tr><td align="center" style="padding:14px 0 4px 0;">'
    '<a href="__URL__" target="_blank" style="display:inline-block;'
    'padding:12px 28px;background:#2e7d32;color:#ffffff;font-size:15px;'
    'font-weight:bold;border-radius:6px;text-decoration:none;">'
    'View interactive flipbook &#8594;</a></td></tr>\n'
    '    </table>\n'
    '  </td></tr>\n'
    '</table>'
)


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
    public_url = fields.Char(
        string='Public Link', compute='_compute_public_url',
        help='Shareable page-flip viewer link. Only resolves once the flipbook '
             'is published; send this URL to vendors/customers.')

    # Email embed (every page rasterised to a PNG, stacked in email-safe HTML).
    render_page_ids = fields.One2many(
        'mint.flipbook.render', 'flipbook_id', string='Rendered Pages', copy=False)
    email_embed_html = fields.Text(
        string='Email Embed HTML', readonly=True, copy=False,
        help="Email-safe HTML that renders every page inline. Generate it while "
             "published, then paste into your email campaign's HTML / code view. "
             "Images load from the public viewer host; the page-flip animation "
             "lives behind the 'View interactive flipbook' link (email clients "
             "strip JavaScript).")

    @api.depends('access_token', 'state')
    def _compute_public_url(self):
        base = (self.env['ir.config_parameter'].sudo().get_param('web.base.url') or '').rstrip('/')
        for rec in self:
            rec.public_url = (
                '%s/flipbook/%s' % (base, rec.access_token)
                if (rec.state == 'published' and rec.access_token) else False)

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
            label = page.name or page.filename or _("untitled page")
            mime = page.mimetype or mimetypes.guess_type(page.filename or '')[0] or ''
            try:
                raw = base64.b64decode(page.file)
                if mime == 'application/pdf':
                    pdf_blobs.append(raw)
                elif mime.startswith('image/'):
                    pdf_blobs.append(self._image_to_pdf(raw))
                else:
                    skipped.append(label)
            except Exception:
                _logger.exception("mint_flipbook: failed to process page %s", page.id)
                raise UserError(_(
                    "Could not process page '%s' — the file may be corrupt or an "
                    "unsupported format.") % label)
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
        # Return True (not an action) so the web client reloads the record and
        # the generated-PDF download link appears immediately.
        return True

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

    # ---------------------------------------------------------------- Email embed
    def action_generate_email_embed(self):
        """Rasterise every page to a PNG and build email-safe embed HTML.

        Email clients strip JS/iframes, so the interactive page-flip viewer
        can't run inline. Instead each page (image pages as-is, PDF pages —
        including multi-page PDFs — rendered via PyMuPDF) becomes a PNG served
        from the public, token-gated /flipbook/<token>/img/<i> route, and the
        pages are stacked in a table-layout, inline-styled HTML block marketing
        can paste straight into a campaign. Mirrors action_generate_pdf: stores
        the artifact on the record and posts to the chatter.
        """
        self.ensure_one()
        if self.state != 'published':
            raise UserError(_(
                "Publish the flipbook first — the email embed links to the "
                "public viewer, which only serves published flipbooks."))
        if not self.page_ids:
            raise UserError(_("Add at least one page before generating the email embed."))
        pngs = self._render_page_pngs()
        if not pngs:
            raise UserError(_("No image or PDF pages to embed."))
        self.render_page_ids.unlink()
        Render = self.env['mint.flipbook.render']
        for i, png in enumerate(pngs):
            Render.create({
                'flipbook_id': self.id,
                'sequence': i,
                'image': base64.b64encode(png),
            })
        # Refresh the one2many cache so _build_email_embed sees the new rows.
        self.invalidate_recordset(['render_page_ids'])
        self.email_embed_html = self._build_email_embed()
        self.message_post(body=_(
            "Generated email embed (%d page image(s)). Copy the HTML into your "
            "email campaign's code view.") % len(pngs))
        # Return True (not an action) so the web client reloads and the embed
        # field appears immediately.
        return True

    def _render_page_pngs(self):
        """Return an ordered list of PNG bytes, one per visual page.

        Image pages are normalised; PDF pages are rendered page-by-page so a
        multi-page sell-sheet expands into all its pages. Unsupported files are
        skipped (parity with action_generate_pdf).
        """
        self.ensure_one()
        pngs = []
        fitz = None
        for page in self.page_ids.sorted(lambda p: (p.sequence, p.id)):
            if not page.file:
                continue
            label = page.name or page.filename or _("untitled page")
            mime = page.mimetype or mimetypes.guess_type(page.filename or '')[0] or ''
            try:
                raw = base64.b64decode(page.file)
                if mime == 'application/pdf':
                    if fitz is None:
                        fitz = self._load_fitz()
                    doc = fitz.open(stream=raw, filetype='pdf')
                    try:
                        for pdf_page in doc:
                            # Matrix(2,2) ≈ 144 DPI — crisp at the 600px email width
                            # (and on retina); _normalize_png caps the final width.
                            pix = pdf_page.get_pixmap(matrix=fitz.Matrix(2, 2))
                            pngs.append(self._normalize_png(pix.tobytes('png')))
                    finally:
                        doc.close()
                elif mime.startswith('image/'):
                    pngs.append(self._normalize_png(raw))
            except UserError:
                raise
            except Exception:
                _logger.exception(
                    "mint_flipbook: email-embed render failed for page %s", page.id)
                raise UserError(_(
                    "Could not render page '%s' for the email embed — the file may "
                    "be corrupt or an unsupported format.") % label)
        return pngs

    @staticmethod
    def _load_fitz():
        """Import PyMuPDF (modern ``pymupdf`` or legacy ``fitz`` name)."""
        try:
            import fitz
            return fitz
        except ImportError:
            try:
                import pymupdf as fitz
                return fitz
            except ImportError:
                raise UserError(_(
                    "PDF pages require the 'pymupdf' library, which is not "
                    "installed on this server. Ask an administrator to install "
                    "it, or upload those pages as images instead."))

    @staticmethod
    def _normalize_png(raw, max_width=1200):
        """Decode any image bytes to RGB PNG, capped to ``max_width`` wide."""
        from PIL import Image
        img = Image.open(io.BytesIO(raw))
        if img.mode not in ('RGB', 'L'):
            img = img.convert('RGB')
        if img.width > max_width:
            ratio = max_width / float(img.width)
            img = img.resize(
                (max_width, max(1, int(img.height * ratio))), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        return buf.getvalue()

    def _build_email_embed(self):
        """Assemble the email-safe HTML from the stored rendered pages."""
        self.ensure_one()
        base = (self.env['ir.config_parameter'].sudo()
                .get_param('web.base.url') or '').rstrip('/')
        token = self.access_token or ''
        url = '%s/flipbook/%s' % (base, token)
        name = str(escape(self.name or 'Flipbook'))
        rows = []
        for rp in self.render_page_ids.sorted(lambda r: (r.sequence, r.id)):
            src = '%s/flipbook/%s/img/%d' % (base, token, rp.sequence)
            alt = str(escape('%s — page %d' % (self.name or 'Flipbook', rp.sequence + 1)))
            rows.append(
                _EMAIL_IMG_ROW
                .replace('__URL__', url)
                .replace('__SRC__', src)
                .replace('__ALT__', alt))
        return (
            _EMAIL_TEMPLATE
            .replace('__NAME__', name)
            .replace('__ROWS__', '\n'.join(rows))
            .replace('__URL__', url))


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


class MintFlipbookRender(models.Model):
    """One rasterised PNG per visual page, produced for the email embed.

    Regenerated wholesale by ``mint.flipbook.action_generate_email_embed`` and
    streamed publicly (token-gated, published-only) by the /flipbook/<token>/img
    route so email clients can load each page as an external image.
    """
    _name = 'mint.flipbook.render'
    _description = 'Flipbook Rendered Page (email embed)'
    _order = 'sequence, id'

    flipbook_id = fields.Many2one(
        'mint.flipbook', string='Flipbook', required=True,
        ondelete='cascade', index=True)
    company_id = fields.Many2one(
        related='flipbook_id.company_id', store=True, index=True)
    sequence = fields.Integer(default=0)
    image = fields.Binary(string='Rendered PNG', attachment=True, required=True)
