"""Integration tests for the flipbook email-embed export.

Codifies the behaviour verified end-to-end on staging (task: marketing email
embed): all pages rasterised to PNGs, stacked into email-safe HTML, and served
over the public token-gated /flipbook/<token>/img/<i> route. Run with:

    odoo -u mint_flipbook --test-enable --test-tags /mint_flipbook
"""
import base64
import io

from odoo.exceptions import UserError
from odoo.tests import HttpCase, TransactionCase, tagged

PNG_MAGIC = b'\x89PNG\r\n\x1a\n'


def _png_bytes(w=800, h=600):
    """A valid in-memory PNG (PIL is always present in the Odoo image)."""
    from PIL import Image
    buf = io.BytesIO()
    Image.new('RGB', (w, h), (46, 125, 50)).save(buf, format='PNG')
    return buf.getvalue()


def _pdf_bytes(pages=3):
    """A multi-page PDF via PyMuPDF, or None if the lib is unavailable."""
    try:
        import fitz
    except ImportError:
        try:
            import pymupdf as fitz
        except ImportError:
            return None
    doc = fitz.open()
    for i in range(pages):
        doc.new_page(width=300, height=400)
    return doc.tobytes()


@tagged('post_install', '-at_install')
class TestFlipbookEmailEmbed(TransactionCase):
    def setUp(self):
        super().setUp()
        self.flipbook = self.env['mint.flipbook'].create({'name': 'Acme <Spring> & Co.'})
        self.env['mint.flipbook.page'].create({
            'flipbook_id': self.flipbook.id, 'sequence': 1, 'name': 'Cover',
            'file': base64.b64encode(_png_bytes()), 'filename': 'cover.png',
        })

    def test_requires_published(self):
        """Embed generation is gated on the published state."""
        with self.assertRaises(UserError):
            self.flipbook.action_generate_email_embed()

    def test_requires_pages(self):
        empty = self.env['mint.flipbook'].create({'name': 'Empty'})
        empty.action_publish()
        with self.assertRaises(UserError):
            empty.action_generate_email_embed()

    def test_generate_embed_image_page(self):
        """One image page -> one render row + email-safe HTML."""
        self.flipbook.action_publish()
        self.flipbook.action_generate_email_embed()
        self.assertEqual(len(self.flipbook.render_page_ids), 1)
        render = self.flipbook.render_page_ids
        self.assertEqual(base64.b64decode(render.image)[:8], PNG_MAGIC)

        html = self.flipbook.email_embed_html or ''
        self.assertTrue(html, "email_embed_html should be populated")
        self.assertEqual(html.count('<img '), 1)
        token = self.flipbook.access_token
        self.assertIn('/flipbook/%s/img/0' % token, html)
        self.assertIn('/flipbook/%s' % token, html)          # viewer link
        self.assertIn('&lt;Spring&gt;', html)                # title HTML-escaped
        self.assertNotIn('<Spring>', html)

    def test_pdf_multipage_expands(self):
        """A 3-page PDF expands into 3 render rows (+1 for the image page)."""
        pdf = _pdf_bytes(3)
        if pdf is None:
            self.skipTest("pymupdf not installed")
        self.env['mint.flipbook.page'].create({
            'flipbook_id': self.flipbook.id, 'sequence': 2, 'name': 'Sheet',
            'file': base64.b64encode(pdf), 'filename': 'sheet.pdf',
        })
        self.flipbook.action_publish()
        self.flipbook.action_generate_email_embed()
        self.assertEqual(len(self.flipbook.render_page_ids), 4)
        self.assertEqual((self.flipbook.email_embed_html or '').count('<img '), 4)

    def test_regenerate_replaces_rows(self):
        """Regenerating wipes the old render rows rather than accumulating."""
        self.flipbook.action_publish()
        self.flipbook.action_generate_email_embed()
        self.flipbook.action_generate_email_embed()
        self.assertEqual(len(self.flipbook.render_page_ids), 1)


@tagged('post_install', '-at_install')
class TestFlipbookImgRoute(HttpCase):
    def test_public_img_route(self):
        flipbook = self.env['mint.flipbook'].create({'name': 'Route Test'})
        self.env['mint.flipbook.page'].create({
            'flipbook_id': flipbook.id, 'file': base64.b64encode(_png_bytes()),
            'filename': 'c.png',
        })
        flipbook.action_publish()
        flipbook.action_generate_email_embed()
        token = flipbook.access_token

        # published page image -> 200 image/png with PNG magic
        resp = self.url_open('/flipbook/%s/img/0' % token)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers.get('Content-Type'), 'image/png')
        self.assertEqual(resp.content[:8], PNG_MAGIC)

        # unknown token -> 404 (route exists, not a 500)
        self.assertEqual(self.url_open('/flipbook/deadbeef/img/0').status_code, 404)

        # out-of-range index -> 404
        self.assertEqual(self.url_open('/flipbook/%s/img/99' % token).status_code, 404)

        # back to draft -> the public route stops serving
        flipbook.action_reset_draft()
        self.assertEqual(self.url_open('/flipbook/%s/img/0' % token).status_code, 404)
