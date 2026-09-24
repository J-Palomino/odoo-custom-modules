"""Trigger endpoint for the JotForm vendor-deal sync.

A daisy.plus Marketing agency POSTs here on a cron (tighter during peak
submission hours). The endpoint only *triggers* the idempotent import in
models/jotform_sync.py — the JotForm API key never leaves Odoo, so a leaked
trigger secret can at worst run an extra sync.

    POST /mint_cc/jotform/sync          X-Api-Key: <mint_cc.jotform.trigger_secret>
    POST /mint_cc/jotform/sync?dry=1    same, forced dry run

Blank secret = endpoint disabled (503), mirroring the other shared-secret
routes (mint_inventory_ops/controllers/inventory_api.py).
"""
import hmac
import json
import logging

from odoo import http
from odoo.http import request, Response

_logger = logging.getLogger(__name__)

PARAM_TRIGGER_SECRET = 'mint_cc.jotform.trigger_secret'


def _json(data, status=200):
    return Response(json.dumps(data, default=str), status=status,
                    content_type='application/json')


class JotformSyncController(http.Controller):

    @http.route('/mint_cc/jotform/sync', type='http', auth='none',
                methods=['POST'], csrf=False)
    def jotform_sync(self, dry=None, **kw):
        expected = request.env['ir.config_parameter'].sudo().get_param(
            PARAM_TRIGGER_SECRET, '')
        if not expected:
            return _json({'error': 'disabled'}, 503)
        key = request.httprequest.headers.get('X-Api-Key', '')
        # Constant-time compare (Odoo #675).
        if not (key and hmac.compare_digest(key, expected)):
            return _json({'error': 'Unauthorized'}, 401)
        try:
            summary = request.env['mint.deal.submission'].sudo()._jotform_sync(
                dry_run=dry in ('1', 'true', 'yes'))
        except Exception as e:
            _logger.exception('jotform_sync trigger failed')
            return _json({'error': str(e)}, 500)
        return _json(summary)
