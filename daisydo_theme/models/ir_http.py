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
        info["company_states"] = self._company_state_map()
        root = self._company_root()
        if root:
            info["company_root_id"] = root.id
            info["company_root_name"] = root.name
        self._sync_company_selection()
        return info

    def _company_state_map(self):
        """Map each company the user may access to its US state name.

        Consumed by the company switcher (switch_company_states.esm.js) to
        group the company list under state subheaders. Only companies with a
        ``state_id`` are included; the rest fall back to an "Other" group on
        the client. Display-only — does not touch the multi-company tree.
        """
        if not request:
            return {}
        user = self.env.user
        if not user or not user._is_internal():
            return {}
        states = {}
        for company in user.company_ids:
            if company.state_id:
                states[company.id] = company.state_id.name
        return states

    def _company_root(self):
        """Top-most ancestor company of the user's state-bearing stores.

        The switcher renders this as a "Mint Cannabis" master row above the
        state groups whose checkbox selects every company. Derived by climbing
        each state store to its no-parent ancestor and taking the most common
        one (resolves to "Mint Cannabis", id=1). Returns an empty recordset
        when the user has no state companies.
        """
        if not request:
            return self.env["res.company"].browse()
        user = self.env.user
        if not user or not user._is_internal():
            return self.env["res.company"].browse()
        counts = {}
        for company in user.company_ids.filtered("state_id"):
            node = company
            while node.parent_id:
                node = node.parent_id
            counts[node.id] = counts.get(node.id, 0) + 1
        if not counts:
            return self.env["res.company"].browse()
        root_id = max(counts, key=counts.get)
        return self.env["res.company"].browse(root_id)

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
