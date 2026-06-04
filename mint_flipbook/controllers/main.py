from odoo import http
from odoo.http import request


class FlipbookViewer(http.Controller):
    """Public, token-addressed viewer for mint.flipbook.

    Access is public + token-only: there is no Odoo login. The unguessable
    ``access_token`` together with ``state == 'published'`` is the sole gate.
    Records are read via ``sudo()`` because mint.flipbook grants no public
    ACL (security/ir.model.access.csv). The controller never exposes internal
    fields such as ``description`` ("Internal Notes") to the public page
    (task #94304 AC09; cf. #93671). Pattern precedent: onlyoffice_odoo
    controllers/controllers.py:166-170 (public route + 404 + binary stream).
    """

    def _get_published(self, access_token):
        """Return the single published flipbook for this token, or None.

        Strict domain on BOTH token and state so the URL alone can never
        widen the scope to draft or other-workspace records.
        """
        if not access_token:
            return None
        flipbook = request.env['mint.flipbook'].sudo().search([
            ('access_token', '=', access_token),
            ('state', '=', 'published'),
        ], limit=1)
        return flipbook or None

    @http.route('/flipbook/<string:access_token>', type='http', auth='public',
                website=True, sitemap=False)
    def flipbook_viewer(self, access_token, **kwargs):
        flipbook = self._get_published(access_token)
        if not flipbook:
            return request.not_found()
        # Whitelist what reaches the public page. Only the title and the PDF
        # URL are passed; internal fields (description / "Internal Notes",
        # vendor, notes) are never handed to the template.
        values = {
            'flipbook_name': flipbook.name or '',
            'pdf_url': '/flipbook/%s/pdf' % access_token,
            'has_pdf': bool(flipbook.pdf_file),
        }
        return request.render('mint_flipbook.flipbook_viewer', values)

    @http.route('/flipbook/<string:access_token>/pdf', type='http', auth='public',
                sitemap=False)
    def flipbook_pdf(self, access_token, **kwargs):
        flipbook = self._get_published(access_token)
        if not flipbook or not flipbook.pdf_file:
            # Published-but-not-generated is a valid state (action_publish does
            # not require a PDF) -> 404; the viewer shows a "not ready" message.
            return request.not_found()
        stream = request.env['ir.binary']._get_stream_from(flipbook, 'pdf_file')
        response = stream.get_response(as_attachment=False)
        response.headers['Content-Type'] = 'application/pdf'
        return response
