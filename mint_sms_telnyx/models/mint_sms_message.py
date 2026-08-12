# -*- coding: utf-8 -*-
"""
Outbound text messages via the BlueBubbles iMessage gateway.

This is the SECOND transport for this module (Telnyx being the first, see
sms_sms.py). It exists because the live outbound channel is an iMessage
gateway (BlueBubbles running on a Mac), not carrier SMS.

The whole point of routing through this model — instead of letting an agent
POST to BlueBubbles directly — is a DETERMINISTIC send gate that runs in Odoo,
not in an LLM prompt. A message is only delivered when the recipient:

    1. resolves to a res.partner, AND
    2. carries the "SMS Whitelist" partner category, AND
    3. is NOT opted out (res.partner.sms_opt_out), AND
    4. has opted in (res.partner.sms_opt_in), AND
    5. has consented to this message's CATEGORY (marketing vs transactional —
       res.partner.sms_consent_{category}, MR-1250), AND
    6. passes the cannabis-content guardrail (reused from sms.sms).

HUGO and other agents send a text simply by creating a record of this model
via the Odoo MCP `create_record` action — the gate + transport run here. The
record itself is the audit row.
"""
import json
import logging
import uuid

import requests

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

BB_TIMEOUT = 20  # seconds — BlueBubbles round-trips an AppleScript send


class MintSmsMessage(models.Model):
    _name = "mint.sms.message"
    _description = "Mint Outbound Text (iMessage via BlueBubbles)"
    _order = "create_date desc"

    partner_id = fields.Many2one(
        "res.partner",
        string="Recipient",
        index=True,
        ondelete="set null",
        help="Recipient contact. If left blank, resolved from Number.",
    )
    number = fields.Char(
        string="Number (E.164)",
        help="Destination in +1XXXXXXXXXX form. If blank, taken from the "
             "recipient's sanitized phone.",
    )
    body = fields.Text(required=True)
    category = fields.Selection(
        [("transactional", "Transactional"), ("marketing", "Marketing")],
        default="transactional",
        required=True,
        index=True,
        help="Which consent bucket this text falls under. The send gate "
             "requires the recipient's matching per-category consent "
             "(res.partner.sms_consent_*).",
    )
    channel = fields.Selection(
        [("imessage", "iMessage (BlueBubbles)")],
        default="imessage",
        required=True,
    )
    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("blocked", "Blocked by gate"),
            ("queued", "Queued"),
            ("sent", "Sent"),
            ("failed", "Failed"),
        ],
        default="draft",
        index=True,
        copy=False,
    )
    block_reason = fields.Char(readonly=True, copy=False)
    chat_guid = fields.Char(readonly=True, copy=False)
    temp_guid = fields.Char(readonly=True, copy=False)
    error_message = fields.Char(readonly=True, copy=False)
    response_payload = fields.Text(readonly=True, copy=False)
    sent_at = fields.Datetime(readonly=True, copy=False)

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------
    @api.model
    def _bb_config(self):
        ICP = self.env["ir.config_parameter"].sudo()
        return {
            "enabled": ICP.get_param("mint_sms_telnyx.bluebubbles_enabled") == "True",
            "url": (ICP.get_param("mint_sms_telnyx.bluebubbles_url", "") or "").strip(),
            "password": ICP.get_param("mint_sms_telnyx.bluebubbles_password", ""),
        }

    @api.model
    def _whitelist_category(self):  # noqa: D401
        """Return the 'SMS Whitelist' partner category recordset (or empty)."""
        ICP = self.env["ir.config_parameter"].sudo()
        cat_id = ICP.get_param("mint_sms_telnyx.whitelist_category_id")
        Cat = self.env["res.partner.category"].sudo()
        if cat_id:
            cat = Cat.browse(int(cat_id)).exists()
            if cat:
                return cat
        return Cat.search([("name", "=", "SMS Whitelist")], limit=1)

    # ------------------------------------------------------------------
    # Recipient resolution + gate
    # ------------------------------------------------------------------
    def _resolve_recipient(self):
        """Return (partner, e164_number). Either may be falsy."""
        self.ensure_one()
        Partner = self.env["res.partner"].sudo()
        partner = self.partner_id
        number = (self.number or "").strip()
        if partner and not number:
            number = partner._sms_e164()
        if not partner and number:
            # A number can match many partners (Dutchie-backfill duplicates).
            # Prefer, in order: whitelisted+consented > any consented >
            # internal-user record > any user record > first match. The old
            # "prefer any user_ids" heuristic picked portal-user dupes over
            # the consented contact (live incident: message #28, MR-1250).
            matches = Partner.search([("phone_sanitized", "=", number)])
            consented = matches.filtered(
                lambda p: p.sms_opt_in and not p.sms_opt_out)
            cat = self._whitelist_category()
            whitelisted = consented.filtered(
                lambda p: cat and cat.id in p.category_id.ids)
            internal = matches.filtered(
                lambda p: any(not u.share for u in p.user_ids))
            partner = (whitelisted[:1] or consented[:1] or internal[:1]
                       or matches.filtered(lambda p: p.user_ids)[:1]
                       or matches[:1])
        return partner, number

    def _check_sendable(self, partner, number):
        """Deterministic gate. Return (ok: bool, reason: str|None)."""
        self.ensure_one()
        if not number:
            return False, "No destination number on the message or recipient."
        if not partner:
            return False, "No Odoo contact matches this number — not whitelisted."

        cat = self._whitelist_category()
        if not cat or cat.id not in partner.category_id.ids:
            return False, "Recipient is not on the Odoo SMS Whitelist."

        if partner.sms_opt_out:
            return False, "Recipient has opted out of texts (sms_opt_out)."

        if not partner.sms_opt_in:
            return False, "Recipient has not opted in to texts (sms_opt_in)."

        if not partner["sms_consent_%s" % self.category]:
            return False, (
                "Recipient has not consented to %s texts "
                "(sms_consent_%s)." % (self.category, self.category))

        # Reuse the cannabis-content guardrail from the Telnyx transport so
        # there's a single blocklist + scan-mode setting for the module.
        Sms = self.env["sms.sms"].sudo()
        cfg = Sms._telnyx_config()
        allowed, matched = Sms._telnyx_scan_content(self.body, cfg)
        if not allowed and cfg["scan_mode"] != "warn":
            return False, f"Content guardrail: blocked on term '{matched}'."

        return True, None

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------
    def _bb_send(self, cfg, number):
        """POST one message to BlueBubbles. Return (ok, status, body_text)."""
        self.ensure_one()
        temp_guid = "mint-%s" % uuid.uuid4().hex
        chat_guid = "iMessage;-;%s" % number
        payload = {
            "chatGuid": chat_guid,
            "message": self.body or "",
            "method": "apple-script",
            "tempGuid": temp_guid,
        }
        self.write({"chat_guid": chat_guid, "temp_guid": temp_guid})
        try:
            resp = requests.post(
                cfg["url"].rstrip("/") + "/api/v1/message/text",
                params={"password": cfg["password"]},
                headers={
                    "Content-Type": "application/json",
                    "ngrok-skip-browser-warning": "true",
                },
                data=json.dumps(payload),
                timeout=BB_TIMEOUT,
            )
        except requests.RequestException as e:
            return False, None, str(e)
        return resp.status_code < 400, resp.status_code, resp.text[:4000]

    def action_send(self):
        """Run the gate and deliver. Safe to call on a recordset."""
        for msg in self:
            if msg.state == "sent":
                continue
            cfg = msg._bb_config()
            if not (cfg["enabled"] and cfg["url"] and cfg["password"]):
                msg.write({
                    "state": "failed",
                    "error_message": "iMessage gateway not enabled/configured.",
                })
                continue

            partner, number = msg._resolve_recipient()
            ok, reason = msg._check_sendable(partner, number)
            if not ok:
                _logger.info("mint.sms.message %s blocked: %s", msg.id, reason)
                msg.write({
                    "state": "blocked",
                    "block_reason": reason,
                    "partner_id": partner.id if partner else msg.partner_id.id,
                    "number": number or msg.number,
                })
                continue

            sent_ok, status, resp_text = msg._bb_send(cfg, number)
            vals = {
                "partner_id": partner.id,
                "number": number,
                "response_payload": resp_text,
            }
            if sent_ok:
                vals.update({"state": "sent", "sent_at": fields.Datetime.now()})
            else:
                vals.update({
                    "state": "failed",
                    "error_message": ("HTTP %s" % status) if status else (resp_text or "send error")[:200],
                })
                _logger.warning("mint.sms.message %s send failed (%s): %s",
                                msg.id, status, (resp_text or "")[:200])
            msg.write(vals)
        return True

    # ------------------------------------------------------------------
    # Auto-send on create (agents call create_record and expect delivery)
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        if not self.env.context.get("mint_sms_no_autosend"):
            records.filtered(lambda m: m.state == "draft").action_send()
        return records
