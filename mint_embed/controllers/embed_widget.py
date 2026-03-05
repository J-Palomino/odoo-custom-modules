# -*- coding: utf-8 -*-
"""
Serves the embed widget JavaScript at /embed/mint-widget.js.
"""
import logging
import os

from odoo import http
from odoo.http import Response

_logger = logging.getLogger(__name__)

_JS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    'static', 'src', 'js', 'mint-widget.js',
)


class MintEmbedWidget(http.Controller):

    @http.route('/embed/mint-widget.js', type='http', auth='none',
                methods=['GET'], csrf=False, cors='*')
    def serve_widget(self, **kwargs):
        try:
            with open(_JS_PATH, 'r') as f:
                js = f.read()
            return Response(
                js,
                status=200,
                content_type='application/javascript; charset=utf-8',
                headers={
                    'Access-Control-Allow-Origin': '*',
                    'Cache-Control': 'public, max-age=3600',
                },
            )
        except Exception as e:
            _logger.error('Error serving embed widget: %s', e)
            return Response(
                '/* Error loading widget */',
                status=500,
                content_type='application/javascript',
            )
