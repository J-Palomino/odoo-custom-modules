# -*- coding: utf-8 -*-
"""
Override sms.sms._send() to route outbound through Telnyx — transactional only.

Hard guardrail: any outbound body containing cannabis terminology is blocked
before it hits Telnyx. The blocklist + scan mode are runtime-configurable via
ir.config_parameter, so admins can tune without redeploying.

Falls through to Odoo's default (IAP) path when the gateway isn't enabled or
isn't configured. Installing the module without keys won't break existing sends.
"""
import json
import logging

import requests

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

TELNYX_TIMEOUT = 15  # seconds — keep tight; runs inside the SMS queue cron

# Default cannabis-content blocklist. Substring, case-insensitive.
# Curated to be conservative; "mint", "cart", "oz" intentionally omitted to
# avoid false positives on company name / shopping cart / Oz. Tune via the
# ir.config_parameter `mint_sms_telnyx.content_blocklist`.
DEFAULT_BLOCKLIST = (
    "cannabis,marijuana,weed,thc,cbd,kush,indica,sativa,"
    "flower,edible,edibles,gummy,gummies,preroll,pre-roll,"
    "concentrate,concentrates,distillate,shatter,rosin,resin,"
    "vape pen,vape cart,disposable vape,disposable pen,"
    "dab,dabs,dabbing,joint,blunt,nug,nugs,bud,buds,"
    "eighth,1/8,quarter ounce,1/4 oz,half ounce,1/2 oz,ounce of,"
    "dispensary,dispensaries,recreational use,medical marijuana,"
    "high thc,high-thc,thca,delta-8,delta 8,delta-9,delta 9,"
    "strain,strains"
)


class SmsSms(models.Model):
    _inherit = "sms.sms"

    telnyx_message_id = fields.Char(index=True, copy=False, readonly=True)

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------
    @api.model
    def _telnyx_config(self):
        ICP = self.env["ir.config_parameter"].sudo()
        return {
            "enabled": ICP.get_param("mint_sms_telnyx.enabled") == "True",
            "api_key": ICP.get_param("mint_sms_telnyx.api_key", ""),
            "api_base": ICP.get_param("mint_sms_telnyx.api_base",
                                     "https://api.telnyx.com/v2"),
            "profile_id": ICP.get_param("mint_sms_telnyx.messaging_profile_id", ""),
            "from_number": ICP.get_param("mint_sms_telnyx.from_number", ""),
            "scan_mode": ICP.get_param("mint_sms_telnyx.scan_mode", "block"),
            "blocklist": ICP.get_param(
                "mint_sms_telnyx.content_blocklist", DEFAULT_BLOCKLIST
            ),
            "block_mailing": ICP.get_param(
                "mint_sms_telnyx.block_mailing_sms", "True"
            ) == "True",
        }

    @api.model
    def _telnyx_should_route(self, cfg):
        return bool(
            cfg["enabled"] and cfg["api_key"]
            and (cfg["profile_id"] or cfg["from_number"])
        )

    # ------------------------------------------------------------------
    # Content guardrail (cannabis terminology → block)
    # ------------------------------------------------------------------
    @api.model
    def _telnyx_scan_content(self, body, cfg):
        """Return (allowed, matched_term). allowed=True when safe to send."""
        if cfg["scan_mode"] == "off":
            return True, None
        text = (body or "").lower()
        if not text:
            return True, None
        terms = [t.strip().lower() for t in (cfg["blocklist"] or "").split(",") if t.strip()]
        for term in terms:
            if term in text:
                return False, term
        return True, None

    @api.model
    def _telnyx_is_marketing(self, sms):
        """True when this sms.sms originated from a mass_mailing_sms campaign."""
        # mailing_id field exists only when mass_mailing_sms is installed.
        return bool(getattr(sms, "mailing_id", False))

    # ------------------------------------------------------------------
    # Outbound override
    # ------------------------------------------------------------------
    def _send(self, unlink_failed=False, unlink_sent=True, raise_exception=False):
        """Route SMS through Telnyx when configured; otherwise fall back to IAP."""
        cfg = self._telnyx_config()
        if not self._telnyx_should_route(cfg):
            return super()._send(
                unlink_failed=unlink_failed,
                unlink_sent=unlink_sent,
                raise_exception=raise_exception,
            )

        Log = self.env["telnyx.message.log"].sudo()
        for sms in self:
            # 1) Opt-out check
            if sms.partner_id and sms.partner_id.sms_opt_out:
                sms.write({"state": "canceled", "failure_type": "sms_optout"})
                Log.create({
                    "direction": "outbound",
                    "sms_sms_id": sms.id,
                    "partner_id": sms.partner_id.id,
                    "to_number": sms.number,
                    "body": sms.body,
                    "state": "error",
                    "error_message": "Partner opted out of SMS",
                })
                continue

            # 2) Refuse marketing-channel sends (mass_mailing_sms)
            if cfg["block_mailing"] and self._telnyx_is_marketing(sms):
                _logger.warning(
                    "Telnyx: refusing marketing SMS (sms.id=%s, mailing_id=%s) — "
                    "transactional-only gateway",
                    sms.id, getattr(sms, "mailing_id", False),
                )
                sms.write({"state": "error", "failure_type": "sms_server"})
                Log.create({
                    "direction": "outbound",
                    "sms_sms_id": sms.id,
                    "partner_id": sms.partner_id.id if sms.partner_id else False,
                    "to_number": sms.number,
                    "body": sms.body,
                    "state": "error",
                    "error_code": "MARKETING_BLOCKED",
                    "error_message": "Mailing-channel SMS refused (transactional-only gateway)",
                })
                continue

            # 3) Content scanner (cannabis terminology)
            allowed, matched = self._telnyx_scan_content(sms.body, cfg)
            if not allowed:
                msg = (
                    f"Cannabis term '{matched}' detected — transactional templates "
                    "must not include product/marketing language. Send blocked."
                )
                _logger.warning("Telnyx content guardrail: sms.id=%s blocked on '%s'",
                                sms.id, matched)
                if cfg["scan_mode"] == "warn":
                    # Log the hit but allow the send (admin escape hatch).
                    Log.create({
                        "direction": "outbound",
                        "sms_sms_id": sms.id,
                        "partner_id": sms.partner_id.id if sms.partner_id else False,
                        "to_number": sms.number,
                        "body": sms.body,
                        "state": "queued",
                        "error_code": "CONTENT_WARN",
                        "error_message": msg,
                    })
                else:  # block (default)
                    sms.write({"state": "error", "failure_type": "sms_server"})
                    Log.create({
                        "direction": "outbound",
                        "sms_sms_id": sms.id,
                        "partner_id": sms.partner_id.id if sms.partner_id else False,
                        "to_number": sms.number,
                        "body": sms.body,
                        "state": "error",
                        "error_code": "CONTENT_BLOCKED",
                        "error_message": msg,
                    })
                    continue

            # 4) Send to Telnyx
            payload = {
                "to": sms.number,
                "text": sms.body or "",
            }
            if cfg["profile_id"]:
                payload["messaging_profile_id"] = cfg["profile_id"]
            if cfg["from_number"]:
                payload["from"] = cfg["from_number"]

            try:
                resp = requests.post(
                    f"{cfg['api_base']}/messages",
                    headers={
                        "Authorization": f"Bearer {cfg['api_key']}",
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                    data=json.dumps(payload),
                    timeout=TELNYX_TIMEOUT,
                )
            except requests.RequestException as e:
                _logger.warning("Telnyx send failed for sms %s: %s", sms.id, e)
                sms.write({"state": "error", "failure_type": "sms_server"})
                Log.log_outbound(sms, None, error=e)
                if raise_exception:
                    raise
                continue

            if resp.status_code >= 400:
                err = resp.text[:500]
                _logger.warning("Telnyx %s for sms %s: %s",
                                resp.status_code, sms.id, err)
                failure_type = "sms_server"
                if resp.status_code == 400:
                    failure_type = "sms_number_format"
                elif resp.status_code in (401, 403):
                    failure_type = "sms_credit"
                sms.write({"state": "error", "failure_type": failure_type})
                Log.log_outbound(sms, None, error=f"HTTP {resp.status_code}: {err}")
                continue

            try:
                body = resp.json()
            except ValueError:
                body = None

            telnyx_id = ((body or {}).get("data") or {}).get("id")
            sms.write({"state": "sent", "telnyx_message_id": telnyx_id})
            Log.log_outbound(sms, body)

        # Housekeeping (mirror Odoo's default behavior)
        if unlink_sent:
            self.filtered(lambda s: s.state == "sent").unlink()
        if unlink_failed:
            self.filtered(lambda s: s.state == "error").unlink()
        return True
