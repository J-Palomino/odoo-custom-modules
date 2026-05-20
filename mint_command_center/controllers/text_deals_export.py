from odoo import http
from odoo.http import request


class TextDealsExportController(http.Controller):

    @http.route(
        '/mint/ptl/<int:day_id>/text-deals',
        type='http',
        auth='user',
        methods=['GET'],
        csrf=False,
    )
    def text_deals(self, day_id, **kwargs):
        """Download a PTL day's deals as text/plain. Suitable for
        bud-tender SMS, POS terminal paste, or social copy."""
        day = request.env['mint.ptl.day'].browse(day_id).exists()
        if not day:
            return request.not_found()

        # Respect record rules: browse returns empty if forbidden
        body = day._compose_text_deals() or ''
        market = day.market_id.code or 'all'
        filename = f"ptl-{day.date}-{market}.txt"
        return request.make_response(
            body,
            headers=[
                ('Content-Type', 'text/plain; charset=utf-8'),
                ('Content-Disposition', f'attachment; filename="{filename}"'),
                ('Cache-Control', 'no-store'),
            ],
        )
