from odoo import models
from odoo.http import request


class IrHttp(models.AbstractModel):
    _inherit = "ir.http"

    def session_info(self):
        info = super().session_info()
        ICP = request.env["ir.config_parameter"].sudo()
        api_url = ICP.get_param("daisy_bot.api_url", "")
        api_key = ICP.get_param("daisy_bot.api_key", "")
        if api_url and api_key:
            info["error_report_api_url"] = api_url
            info["error_report_api_key"] = api_key
        self._sync_company_selection()
        return info

    def _sync_company_selection(self):
        """Make a user's multi-company switcher selection survive logout/login.

        Odoo wipes the ``cids`` cookie on logout (ir.http._post_logout), so a
        fresh login falls back to the user's main company only. We mirror the
        active selection onto ``res.users.last_company_cids`` and restore it as
        the ``cids`` cookie whenever a session starts without one. The web
        client filters the restored ids against the companies the user may
        still access (see web/static/src/core/user.js), so a stale value is
        harmless.
        """
        if not request:
            return
        user = self.env.user
        # Only internal users have the multi-company switcher; portal/public
        # users never set a `cids` cookie.
        if not user or not user._is_internal():
            return

        cookie_cids = request.httprequest.cookies.get("cids")
        if cookie_cids:
            # A selection is active — remember it. Skip on a read-only cursor
            # (e.g. the /web/session/get_session_info route) and only write when
            # the value actually changed to avoid needless updates.
            if not self.env.cr.readonly and user.last_company_cids != cookie_cids:
                user.sudo().last_company_cids = cookie_cids
        elif user.last_company_cids:
            # Fresh session with no cookie — restore the previous selection so
            # the web client boots into the same companies as last time.
            request.future_response.set_cookie("cids", user.last_company_cids)
