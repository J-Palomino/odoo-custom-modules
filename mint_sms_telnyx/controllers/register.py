# -*- coding: utf-8 -*-
"""
BlueBubbles endpoint self-registration.

The BlueBubbles server is exposed through a free-ngrok tunnel whose public URL
rotates whenever the tunnel restarts. Rather than hand-editing the config each
time, the BlueBubbles host POSTs its new URL here and we update
`mint_sms_telnyx.bluebubbles_url` automatically.

    POST /mint_sms/bluebubbles/register
    {"secret": "<shared secret>", "url": "https://new-tunnel.ngrok-free.dev",
     "password": "<optional: also rotate the server password>"}

SECURITY: this repoints where our outbound texts are sent, so it is gated by a
shared secret (constant-time compared) AND the new URL is verified to be a live
BlueBubbles server before it is accepted. Without the secret configured, the
endpoint is disabled.
"""
import hmac
import json
import logging

import requests

from odoo import http
from odoo.http import request, Response

_logger = logging.getLogger(__name__)

VERIFY_TIMEOUT = 10  # seconds — probe the candidate server before trusting it


class BlueBubblesRegister(http.Controller):

    def _json(self, payload, status=200):
        return Response(json.dumps(payload), status=status,
                        content_type="application/json")

    @http.route(
        "/mint_sms/bluebubbles/register",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
        save_session=False,
    )
    def register(self, **kw):
        ICP = request.env["ir.config_parameter"].sudo()
        secret = ICP.get_param("mint_sms_telnyx.bluebubbles_register_secret", "")
        if not secret:
            return self._json({"ok": False, "error": "registration disabled"}, 503)

        try:
            body = json.loads(request.httprequest.get_data(as_text=True) or "{}")
        except ValueError:
            return self._json({"ok": False, "error": "invalid JSON"}, 400)

        provided = body.get("secret") or ""
        if not hmac.compare_digest(str(provided), str(secret)):
            _logger.warning("BlueBubbles register: bad secret from %s",
                            request.httprequest.remote_addr)
            return self._json({"ok": False, "error": "forbidden"}, 403)

        url = (body.get("url") or "").strip().rstrip("/")
        if not url.startswith("https://") and not url.startswith("http://"):
            return self._json({"ok": False, "error": "url must be http(s)"}, 400)

        # Use the new password if supplied, else the currently-stored one, to
        # verify the candidate is a real, reachable BlueBubbles server.
        password = body.get("password") or ICP.get_param(
            "mint_sms_telnyx.bluebubbles_password", "")
        try:
            resp = requests.get(
                url + "/api/v1/server/info",
                params={"password": password},
                headers={"ngrok-skip-browser-warning": "true"},
                timeout=VERIFY_TIMEOUT,
            )
            ok = resp.status_code == 200 and "server_version" in resp.text
        except requests.RequestException as e:
            _logger.warning("BlueBubbles register: verify failed for %s: %s", url, e)
            return self._json({"ok": False, "error": "candidate URL unreachable"}, 400)

        if not ok:
            return self._json(
                {"ok": False, "error": "URL is not a valid BlueBubbles server "
                 "(check password)"}, 400)

        ICP.set_param("mint_sms_telnyx.bluebubbles_url", url)
        if body.get("password"):
            ICP.set_param("mint_sms_telnyx.bluebubbles_password", body["password"])
        _logger.info("BlueBubbles register: URL updated to %s", url)
        return self._json({"ok": True, "url": url})
