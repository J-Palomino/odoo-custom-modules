# -*- coding: utf-8 -*-
"""
Telnyx webhook controller.

Endpoint: POST /telnyx/sms/webhook

Telnyx posts JSON with `data.event_type` set to one of:
  - message.sent
  - message.finalized           (terminal: delivered / delivery_failed / sending_failed)
  - message.received            (inbound SMS)

Signature verification (Ed25519):
  Headers: telnyx-signature-ed25519, telnyx-timestamp
  Signed payload: f"{timestamp}|{raw_body}"
  Public key: per messaging profile, configured in settings.
"""
import base64
import json
import logging
import re
import time

from odoo import fields, http
from odoo.http import request, Response

_logger = logging.getLogger(__name__)

TIMESTAMP_TOLERANCE_SECONDS = 5 * 60  # reject replays older than 5 minutes

STATE_MAP = {
    "queued": "queued",
    "sending": "sending",
    "sent": "sent",
    "delivered": "delivered",
    "delivery_failed": "delivery_failed",
    "sending_failed": "sending_failed",
    "received": "received",
}

ODOO_TERMINAL_OK = {"delivered", "sent"}
ODOO_TERMINAL_FAIL = {"delivery_failed", "sending_failed"}

STOP_KEYWORDS = {"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT"}
START_KEYWORDS = {"START", "UNSTOP", "YES"}


def _digits(s):
    return re.sub(r"\D", "", s or "")


def _normalize_e164(s):
    """Coarse E.164 normalize — keep leading +, strip other non-digits."""
    if not s:
        return ""
    s = s.strip()
    if s.startswith("+"):
        return "+" + _digits(s)
    digits = _digits(s)
    # Heuristic: 10-digit US → prepend +1
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return digits


class TelnyxWebhook(http.Controller):

    @http.route(
        "/telnyx/sms/webhook",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
        save_session=False,
    )
    def webhook(self, **kw):
        raw = request.httprequest.get_data(cache=False, as_text=False) or b""
        signature = request.httprequest.headers.get("telnyx-signature-ed25519", "")
        timestamp = request.httprequest.headers.get("telnyx-timestamp", "")

        if not self._verify_signature(raw, signature, timestamp):
            _logger.warning("Telnyx webhook: signature verification failed")
            return Response("invalid signature", status=401)

        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return Response("bad json", status=400)

        data = (payload.get("data") or {})
        event_type = data.get("event_type") or ""
        event_payload = (data.get("payload") or {})

        try:
            if event_type == "message.received":
                self._handle_inbound(event_payload, raw)
            elif event_type in ("message.sent", "message.finalized"):
                self._handle_dlr(event_type, event_payload, raw)
            else:
                _logger.info("Telnyx webhook: ignoring event_type=%s", event_type)
        except Exception:
            _logger.exception("Telnyx webhook: handler error for %s", event_type)
            # Return 200 so Telnyx doesn't retry on bugs we've already logged.
            # Switch to 5xx if you'd rather have retries.
            return Response("ok", status=200)

        return Response("ok", status=200)

    # ------------------------------------------------------------------
    # Signature verification
    # ------------------------------------------------------------------
    def _verify_signature(self, raw_body, signature_b64, timestamp_str):
        ICP = request.env["ir.config_parameter"].sudo()
        public_key_b64 = ICP.get_param("mint_sms_telnyx.webhook_public_key", "")
        if not public_key_b64:
            _logger.warning("Telnyx webhook: no public key configured; rejecting")
            return False
        if not signature_b64 or not timestamp_str:
            return False

        try:
            ts = int(timestamp_str)
        except ValueError:
            return False
        if abs(time.time() - ts) > TIMESTAMP_TOLERANCE_SECONDS:
            _logger.warning("Telnyx webhook: timestamp outside tolerance")
            return False

        try:
            from nacl.signing import VerifyKey
            from nacl.exceptions import BadSignatureError
        except ImportError:
            _logger.error("Telnyx webhook: PyNaCl not installed; cannot verify")
            return False

        try:
            verify_key = VerifyKey(base64.b64decode(public_key_b64))
            signed = f"{timestamp_str}|".encode("utf-8") + raw_body
            verify_key.verify(signed, base64.b64decode(signature_b64))
            return True
        except BadSignatureError:
            return False
        except Exception:
            _logger.exception("Telnyx webhook: signature verify crashed")
            return False

    # ------------------------------------------------------------------
    # Delivery receipts (outbound)
    # ------------------------------------------------------------------
    def _handle_dlr(self, event_type, payload, raw_body):
        msg_id = payload.get("id")
        if not msg_id:
            return

        env = request.env(su=True)
        Log = env["telnyx.message.log"]
        log = Log.search([("telnyx_message_id", "=", msg_id)], limit=1)

        # Telnyx puts the per-recipient status under `to[].status` for finalized events.
        to_list = payload.get("to") or []
        recipient = to_list[0] if to_list else {}
        telnyx_state = recipient.get("status") or payload.get("status") or ""
        mapped = STATE_MAP.get(telnyx_state, telnyx_state or None)

        vals = {
            "state": mapped,
            "raw_response": json.dumps(payload)[:4000],
        }
        if mapped == "delivered":
            vals["delivered_at"] = fields.Datetime.now()

        errors = payload.get("errors") or []
        if errors:
            err = errors[0]
            vals["error_code"] = str(err.get("code") or "")[:64]
            vals["error_message"] = (err.get("title") or err.get("detail") or "")[:500]

        if log:
            log.write(vals)
            sms = log.sms_sms_id
        else:
            sms = env["sms.sms"].search([("telnyx_message_id", "=", msg_id)], limit=1)

        # Reflect terminal state back onto sms.sms so Odoo's reporting is right.
        if sms:
            if mapped in ODOO_TERMINAL_OK:
                sms.write({"state": "sent"})
            elif mapped in ODOO_TERMINAL_FAIL:
                sms.write({"state": "error", "failure_type": "sms_server"})

    # ------------------------------------------------------------------
    # Inbound SMS
    # ------------------------------------------------------------------
    def _handle_inbound(self, payload, raw_body):
        env = request.env(su=True)
        msg_id = payload.get("id")
        body_text = payload.get("text") or ""
        from_obj = payload.get("from") or {}
        to_list = payload.get("to") or []
        from_number = _normalize_e164(from_obj.get("phone_number"))
        to_number = _normalize_e164((to_list[0] or {}).get("phone_number") if to_list else "")

        partner = self._match_partner(env, from_number)

        env["telnyx.message.log"].create({
            "direction": "inbound",
            "telnyx_message_id": msg_id,
            "partner_id": partner.id if partner else False,
            "from_number": from_number,
            "to_number": to_number,
            "body": body_text,
            "state": "received",
            "raw_response": json.dumps(payload)[:4000],
        })

        # Opt-out / opt-in handling
        keyword = (body_text or "").strip().upper().split()[0] if body_text.strip() else ""
        if keyword in STOP_KEYWORDS and partner:
            partner.write({
                "sms_opt_out": True,
                "sms_opt_out_date": fields.Datetime.now(),
            })
        elif keyword in START_KEYWORDS and partner:
            partner.write({
                "sms_opt_out": False,
                "sms_opt_out_date": False,
            })

        # Route to partner chatter, fallback to configured Discuss channel.
        if partner:
            partner.message_post(
                body=f"<p><strong>SMS from {from_number}</strong></p><p>{body_text}</p>",
                subtype_xmlid="mail.mt_note",
            )
        else:
            channel_id = env["ir.config_parameter"].get_param(
                "mint_sms_telnyx.inbound_channel_id"
            )
            if channel_id:
                channel = env["discuss.channel"].browse(int(channel_id))
                if channel.exists():
                    channel.message_post(
                        body=f"<p><strong>SMS from {from_number}</strong> "
                             f"(no matching partner)</p><p>{body_text}</p>",
                    )

    def _match_partner(self, env, e164):
        if not e164:
            return env["res.partner"]
        digits = _digits(e164)
        last10 = digits[-10:] if len(digits) >= 10 else digits
        # Partners store phone/mobile with varied formatting; match on last 10 digits.
        partner = env["res.partner"].search([
            "|",
            ("mobile", "ilike", last10),
            ("phone", "ilike", last10),
        ], limit=1)
        return partner
