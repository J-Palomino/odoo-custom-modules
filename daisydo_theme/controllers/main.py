import json
import logging
import os

from odoo import http
from odoo.http import request
from odoo.addons.auth_oauth.controllers.main import OAuthLogin

_logger = logging.getLogger(__name__)

_BRAND_NAME = os.environ.get('BRAND_NAME', 'Mint')
_PRIMARY_COLOR = os.environ.get('ODOO_BRAND_PRIMARY',
                                os.environ.get('BRAND_PRIMARY_COLOR', '#00954c'))


def _manifest_base():
    return {
        "name": _BRAND_NAME,
        "short_name": _BRAND_NAME,
        "display": "standalone",
        "theme_color": _PRIMARY_COLOR,
        "background_color": "#ffffff",
        "prefer_related_applications": False,
        "icons": [
            {
                "src": "/daisydo_theme/static/src/img/logo.png",
                "sizes": "192x192",
                "type": "image/png",
            },
            {
                "src": "/daisydo_theme/static/src/img/logo.png",
                "sizes": "512x512",
                "type": "image/png",
            },
        ],
    }


class ThemeController(http.Controller):
    """Serve PWA manifest with branding from env vars."""

    @http.route("/manifest.json", type="http", auth="public")
    def pwa_manifest(self, **kwargs):
        manifest = {**_manifest_base(), "start_url": "/"}
        return request.make_response(
            json.dumps(manifest),
            headers=[("Content-Type", "application/manifest+json")],
        )


class DaisyWebManifest(http.Controller):
    """Override Odoo's backend /web/manifest.webmanifest with branding."""

    @http.route(
        "/web/manifest.webmanifest",
        type="http",
        auth="public",
        methods=["GET"],
    )
    def webmanifest(self, **kwargs):
        manifest = {
            **_manifest_base(),
            "scope": "/odoo",
            "start_url": "/odoo",
            "share_target": {
                "action": "/odoo?share_target=trigger",
                "method": "POST",
                "enctype": "multipart/form-data",
                "params": {
                    "files": [
                        {
                            "name": "externalMedia",
                            "accept": ["image/*", "application/pdf"],
                        }
                    ]
                },
            },
            "shortcuts": [],
        }
        return request.make_response(
            json.dumps(manifest),
            headers=[("Content-Type", "application/manifest+json")],
        )


class DaisyOAuthLogin(OAuthLogin):
    """Fix OAuth redirect_uri to use web.base.url instead of url_root.

    Behind Railway's proxy chain, Werkzeug's url_root returns http://
    even with proxy_mode enabled. This override replaces the computed
    url_root with the canonical web.base.url from ir.config_parameter.
    """

    def list_providers(self):
        providers = super().list_providers()
        base_url = request.env["ir.config_parameter"].sudo().get_param(
            "web.base.url", ""
        ).rstrip("/")
        if base_url:
            url_root = request.httprequest.url_root.rstrip("/")
            if url_root != base_url:
                for p in providers:
                    if "auth_link" in p:
                        p["auth_link"] = p["auth_link"].replace(url_root, base_url)
                _logger.debug("Fixed OAuth redirect_uri: %s -> %s", url_root, base_url)
        return providers
