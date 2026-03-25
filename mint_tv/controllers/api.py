import json

from odoo import http
from odoo.http import request


class TvAPI(http.Controller):

    def _json(self, data, status=200):
        return request.make_response(
            json.dumps(data, default=str),
            headers=[
                ('Content-Type', 'application/json'),
                ('Cache-Control', 'public, max-age=15'),
                ('Access-Control-Allow-Origin', '*'),
            ],
            status=status,
        )

    # --- Layouts ---

    @http.route('/api/v1/tv/layouts', type='http', auth='none',
                methods=['GET'], csrf=False)
    def get_layouts(self, company_id=None, **kw):
        domain = [('active', '=', True)]
        if company_id:
            domain.append(('company_id', '=', int(company_id)))
        layouts = request.env['mint.tv.layout'].sudo().search(domain)
        return self._json([l._to_api_dict() for l in layouts])

    @http.route('/api/v1/tv/layouts/<int:layout_id>', type='http',
                auth='none', methods=['GET'], csrf=False)
    def get_layout(self, layout_id, **kw):
        layout = request.env['mint.tv.layout'].sudo().browse(layout_id).exists()
        if not layout:
            return self._json({'error': 'Layout not found'}, 404)
        return self._json(layout._to_api_dict())

    # --- Books ---

    @http.route('/api/v1/tv/books', type='http', auth='none',
                methods=['GET'], csrf=False)
    def get_books(self, company_id=None, **kw):
        domain = [('active', '=', True)]
        if company_id:
            domain.append(('company_id', '=', int(company_id)))
        books = request.env['mint.tv.book'].sudo().search(domain)
        return self._json([b._to_api_dict() for b in books])

    @http.route('/api/v1/tv/books/<int:book_id>', type='http',
                auth='none', methods=['GET'], csrf=False)
    def get_book(self, book_id, **kw):
        book = request.env['mint.tv.book'].sudo().browse(book_id).exists()
        if not book:
            return self._json({'error': 'Book not found'}, 404)
        return self._json(book._to_api_dict())

    # --- Kiosks ---

    @http.route('/api/v1/tv/kiosks', type='http', auth='none',
                methods=['GET'], csrf=False)
    def get_kiosks(self, company_id=None, **kw):
        domain = [('active', '=', True)]
        if company_id:
            domain.append(('company_id', '=', int(company_id)))
        kiosks = request.env['mint.tv.kiosk'].sudo().search(domain)
        return self._json([k._to_api_dict() for k in kiosks])

    @http.route('/api/v1/tv/kiosks/<int:kiosk_id>', type='http',
                auth='none', methods=['GET'], csrf=False)
    def get_kiosk(self, kiosk_id, **kw):
        kiosk = request.env['mint.tv.kiosk'].sudo().browse(kiosk_id).exists()
        if not kiosk:
            return self._json({'error': 'Kiosk not found'}, 404)
        return self._json(kiosk._to_api_dict())

    # --- Sync endpoint — returns everything in one call ---

    @http.route('/api/v1/tv/sync', type='http', auth='none',
                methods=['GET'], csrf=False)
    def sync_all(self, company_id=None, **kw):
        domain = [('active', '=', True)]
        if company_id:
            domain.append(('company_id', '=', int(company_id)))
        layouts = request.env['mint.tv.layout'].sudo().search(domain)
        books = request.env['mint.tv.book'].sudo().search(domain)
        kiosks = request.env['mint.tv.kiosk'].sudo().search(domain)
        return self._json({
            'layouts': [l._to_api_dict() for l in layouts],
            'books': [b._to_api_dict() for b in books],
            'kiosks': [k._to_api_dict() for k in kiosks],
        })

    # --- CORS preflight ---

    @http.route(['/api/v1/tv/layouts', '/api/v1/tv/books', '/api/v1/tv/kiosks', '/api/v1/tv/sync'],
                type='http', auth='none', methods=['OPTIONS'], csrf=False)
    def preflight(self, **kw):
        return request.make_response('', headers=[
            ('Access-Control-Allow-Origin', '*'),
            ('Access-Control-Allow-Methods', 'GET, OPTIONS'),
            ('Access-Control-Allow-Headers', 'Content-Type'),
            ('Access-Control-Max-Age', '86400'),
        ])
