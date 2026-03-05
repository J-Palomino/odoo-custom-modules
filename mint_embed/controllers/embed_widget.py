# -*- coding: utf-8 -*-
"""
Serves the embed widget JavaScript at /embed/mint-widget.js.

This is a static-ish controller — the JS is read from the module's
static directory and served with permissive CORS + long cache.
"""
import logging
import os

from odoo import http
from odoo.http import Response

_logger = logging.getLogger(__name__)

_JS_CACHE = None


def _read_widget_js():
    global _JS_CACHE
    if _JS_CACHE is None:
        js_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            'static', 'src', 'js', 'mint-widget.js',
        )
        with open(js_path, 'r') as f:
            _JS_CACHE = f.read()
    return _JS_CACHE


class MintEmbedWidget(http.Controller):

    @http.route('/embed/mint-widget.js', type='http', auth='none',
                methods=['GET'], csrf=False, cors='*')
    def serve_widget(self, **kwargs):
        try:
            js = _read_widget_js()
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
                f'/* Error loading widget: {e} */',
                status=500,
                content_type='application/javascript',
            )
