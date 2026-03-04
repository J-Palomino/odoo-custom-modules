import json

from odoo import fields, http
from odoo.http import request


class BannerAPI(http.Controller):

    def _json(self, data, status=200, headers=None):
        resp_headers = {
            'Content-Type': 'application/json',
            'Cache-Control': 'public, max-age=300',
            'Access-Control-Allow-Origin': '*',
        }
        if headers:
            resp_headers.update(headers)
        return request.make_response(
            json.dumps(data, default=str),
            headers=list(resp_headers.items()),
            status=status,
        )

    @http.route('/api/v1/push/banners', type='http', auth='none', methods=['GET'], csrf=False)
    def get_banners(self, slot=None, **kw):
        domain = [('is_active', '=', True)]
        today = fields.Date.today()
        domain += [
            '|', ('active_from', '=', False), ('active_from', '<=', today),
        ]
        domain += [
            '|', ('active_until', '=', False), ('active_until', '>=', today),
        ]
        if slot:
            domain.append(('slot', '=', slot))
        banners = request.env['mint.push.banner'].sudo().search(domain, order='sequence, id')
        return self._json([b._to_api_dict() for b in banners])

    @http.route('/api/v1/push/banners', type='http', auth='none', methods=['OPTIONS'], csrf=False)
    def banners_preflight(self, **kw):
        return request.make_response('', headers=[
            ('Access-Control-Allow-Origin', '*'),
            ('Access-Control-Allow-Methods', 'GET, OPTIONS'),
            ('Access-Control-Allow-Headers', 'Content-Type'),
            ('Access-Control-Max-Age', '86400'),
        ])
