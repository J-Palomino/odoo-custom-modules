"""Daisy Workbench sign-in endpoints. See models/daisy_workbench.py for the flow."""
import json
import logging
import re
from html import escape
from urllib.parse import urlencode

from odoo import http
from odoo.exceptions import AccessError, UserError
from odoo.http import request

_logger = logging.getLogger(__name__)

# The callback is ALWAYS loopback: only the port comes from the caller, so a
# crafted link can never send a code to another machine.
_CALLBACK = "http://127.0.0.1:%d/auth/callback"
_STATE_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


def _valid_port(raw):
    try:
        port = int(raw)
    except (TypeError, ValueError):
        return None
    return port if 1024 <= port <= 65535 else None


def _page(title, body, status=200):
    html = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>%s</title><style>"
        "body{font-family:system-ui,sans-serif;background:#f5f7f6;color:#1d2b24;margin:0;"
        "display:flex;min-height:100vh;align-items:center;justify-content:center;padding:16px}"
        ".card{background:#fff;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,.08);"
        "padding:28px;max-width:420px;width:100%%}"
        "h1{font-size:1.25rem;margin:0 0 12px}p{line-height:1.5;margin:0 0 16px}"
        "code{background:#eef2f0;padding:2px 6px;border-radius:4px}"
        "button{background:#1f7a4d;color:#fff;border:0;border-radius:8px;padding:10px 18px;"
        "font-size:1rem;cursor:pointer}a{color:#1f7a4d}"
        "</style></head><body><div class='card'><h1>%s</h1>%s</div></body></html>"
    ) % (escape(title), escape(title), body)
    return request.make_response(
        html, status=status,
        headers=[("Content-Type", "text/html; charset=utf-8"), ("Cache-Control", "no-store"),
                 ("X-Frame-Options", "DENY")],
    )


def _json(data, status=200):
    return request.make_json_response(data, status=status, headers=[("Cache-Control", "no-store")])


def _json_body():
    try:
        data = json.loads(request.httprequest.get_data(as_text=True) or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


class DaisyWorkbenchController(http.Controller):

    @http.route("/daisy/workbench/connect", type="http", auth="user", methods=["GET"])
    def connect(self, port=None, state=None, host=None, **kw):
        port = _valid_port(port)
        if not port or not state or not _STATE_RE.match(state):
            return _page("Invalid sign-in link",
                         "<p>This link is incomplete. Start again from the Workbench.</p>", 400)
        user = request.env.user
        return _page(
            "Connect Daisy Workbench",
            "<p>Signed in as <strong>%s</strong>.</p>"
            "<p>This gives the Workbench on <code>%s</code> its own Daisy+ key. "
            "Signing out of the Workbench revokes it; IT can also revoke it for you.</p>"
            "<form method='post' action='/daisy/workbench/connect'>"
            "<input type='hidden' name='csrf_token' value='%s'>"
            "<input type='hidden' name='port' value='%d'>"
            "<input type='hidden' name='state' value='%s'>"
            "<input type='hidden' name='host' value='%s'>"
            "<button type='submit'>Connect</button></form>"
            % (escape(user.login), escape(host or "this computer"), escape(request.csrf_token()),
               port, escape(state), escape(host or "")),
        )

    @http.route("/daisy/workbench/connect", type="http", auth="user", methods=["POST"])
    def connect_confirm(self, port=None, state=None, host=None, **kw):
        port = _valid_port(port)
        if not port or not state or not _STATE_RE.match(state):
            return _page("Invalid sign-in link",
                         "<p>This link is incomplete. Start again from the Workbench.</p>", 400)
        try:
            code = request.env["daisy.workbench.token"]._start(request.env.user, host)
        except (AccessError, UserError) as e:
            return _page("Can't connect this Workbench", "<p>%s</p>" % escape(str(e)), 403)
        return request.redirect(
            "%s?%s" % (_CALLBACK % port, urlencode({"code": code, "state": state})),
            code=303, local=False,
        )

    @http.route("/daisy/workbench/token", type="http", auth="public", methods=["POST"],
                csrf=False, save_session=False)
    def token(self, **kw):
        code = _json_body().get("code")
        try:
            payload = request.env["daisy.workbench.token"].sudo()._redeem(code)
        except (AccessError, UserError) as e:
            return _json({"error": "access_denied", "message": str(e)}, 403)
        except Exception:
            _logger.exception("Daisy Workbench token exchange failed")
            return _json({"error": "server_error", "message": "Could not create a Daisy+ key."}, 502)
        if not payload:
            return _json({"error": "invalid_grant",
                          "message": "Sign-in code is invalid or expired."}, 400)
        return _json(payload)

    @http.route("/daisy/workbench/revoke", type="http", auth="public", methods=["POST"],
                csrf=False, save_session=False)
    def revoke(self, **kw):
        auth = request.httprequest.headers.get("Authorization", "")
        api_key = auth[7:].strip() if auth.startswith("Bearer ") else None
        try:
            revoked = request.env["daisy.workbench.token"].sudo()._revoke_by_key(api_key)
        except Exception:
            _logger.exception("Daisy Workbench self-revoke failed")
            return _json({"error": "server_error"}, 502)
        return _json({"revoked": bool(revoked)})
