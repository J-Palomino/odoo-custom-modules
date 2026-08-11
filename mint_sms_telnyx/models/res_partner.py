# -*- coding: utf-8 -*-
import logging

import requests

from odoo import fields, models

_logger = logging.getLogger(__name__)


class ResPartner(models.Model):
    _inherit = "res.partner"

    sms_opt_out = fields.Boolean(
        string="SMS Opt-Out",
        help="Set automatically when this partner texts STOP. "
             "Cleared on UNSTOP/START. Outbound SMS skips opt-out partners.",
        index=True,
    )
    sms_opt_out_date = fields.Datetime(readonly=True)

    # ------------------------------------------------------------------
    # Opt-in (explicit consent — required by the iMessage send gate)
    # ------------------------------------------------------------------
    sms_opt_in = fields.Boolean(
        string="SMS Opt-In",
        help="Explicit consent to receive texts. Required before any send. "
             "Set via internal staff toggle, the customer web form, or a "
             "START/YES keyword reply.",
        index=True,
    )
    sms_opt_in_date = fields.Datetime(readonly=True)
    sms_opt_in_source = fields.Selection(
        [
            ("internal_staff", "Internal Staff"),
            ("external_web", "Customer Web Form"),
            ("sms_keyword", "SMS Keyword (START/YES)"),
            ("manual", "Manual"),
        ],
        string="Opt-In Source",
        readonly=True,
    )

    # ------------------------------------------------------------------
    # Helpers — single place that mutates opt-in/out state + whitelist tag
    # ------------------------------------------------------------------
    def set_sms_opt_in(self, source="manual", add_whitelist=True):
        """Grant SMS consent: set opt-in, clear opt-out, add whitelist tag."""
        cat = self.env["mint.sms.message"]._whitelist_category()
        vals = {
            "sms_opt_in": True,
            "sms_opt_in_date": fields.Datetime.now(),
            "sms_opt_in_source": source,
            "sms_opt_out": False,
            "sms_opt_out_date": False,
        }
        if add_whitelist and cat:
            vals["category_id"] = [(4, cat.id)]
        self.write(vals)
        self._sync_proxy_whitelist("add")
        return True

    def set_sms_opt_out(self):
        """Revoke consent: set opt-out, clear opt-in. Whitelist tag retained."""
        self.write({
            "sms_opt_out": True,
            "sms_opt_out_date": fields.Datetime.now(),
            "sms_opt_in": False,
        })
        self._sync_proxy_whitelist("remove")
        return True

    def _sync_proxy_whitelist(self, action):
        """Mirror consent to the BlueBubbles proxy's own delivery whitelist.

        The proxy in front of BlueBubbles keeps a SECOND whitelist and 403s
        sends to numbers not on it — so Odoo-side consent alone leaves texts
        silently failing at delivery (observed 2026-08-11: a fully opted-in,
        Odoo-whitelisted customer got HTTP 403 until her number was added at
        the proxy by hand). Mirroring here makes the client opt-in flow
        end-to-end: consent recorded AND deliverable in one step.

        Strictly best-effort, and called only AFTER the consent write: the
        proxy lives behind a free-ngrok tunnel that comes and goes, and a
        delivery-gate hiccup must never fail or roll back the consent
        record. Failures are logged; the number can be replayed by calling
        this method again (add/remove are idempotent on the proxy —
        "changed" in its response says whether anything moved).
        """
        icp = self.env["ir.config_parameter"].sudo()
        base = (icp.get_param("mint_sms_telnyx.bluebubbles_url") or "").rstrip("/")
        token = icp.get_param("mint_sms_telnyx.proxy_whitelist_token")
        if not base or not token:
            _logger.warning(
                "Proxy whitelist %s skipped: bluebubbles_url or "
                "proxy_whitelist_token not configured", action)
            return
        for partner in self:
            number = partner.phone_sanitized
            if not number:
                _logger.warning(
                    "Proxy whitelist %s skipped for partner %s: no sanitized "
                    "phone (raw %r)", action, partner.id, partner.phone)
                continue
            try:
                resp = requests.post(
                    base + "/admin/whitelist",
                    json={"number": number, "action": action},
                    headers={
                        "Authorization": "Bearer %s" % token,
                        "ngrok-skip-browser-warning": "true",
                    },
                    timeout=10,
                )
                if resp.ok:
                    _logger.info(
                        "Proxy whitelist %s for partner %s (%s): %s",
                        action, partner.id, number, resp.text[:200])
                else:
                    _logger.warning(
                        "Proxy whitelist %s FAILED for partner %s (%s): "
                        "HTTP %s %s", action, partner.id, number,
                        resp.status_code, resp.text[:200])
            except Exception:
                _logger.exception(
                    "Proxy whitelist %s errored for partner %s (%s)",
                    action, partner.id, number)

    # UI buttons (internal staff opt-in flow)
    def action_sms_opt_in(self):
        return self.set_sms_opt_in(source="internal_staff")

    def action_sms_opt_out(self):
        return self.set_sms_opt_out()
