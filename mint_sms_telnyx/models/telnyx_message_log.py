# -*- coding: utf-8 -*-
from odoo import api, fields, models


class TelnyxMessageLog(models.Model):
    _name = "telnyx.message.log"
    _description = "Telnyx SMS Message Log"
    _order = "create_date desc"
    _rec_name = "telnyx_message_id"

    direction = fields.Selection(
        [("outbound", "Outbound"), ("inbound", "Inbound")],
        required=True,
        index=True,
    )
    telnyx_message_id = fields.Char(string="Telnyx Message ID", index=True)
    sms_sms_id = fields.Many2one(
        "sms.sms",
        string="Odoo SMS",
        ondelete="set null",
        index=True,
    )
    partner_id = fields.Many2one("res.partner", ondelete="set null", index=True)
    from_number = fields.Char(index=True)
    to_number = fields.Char(index=True)
    body = fields.Text()
    state = fields.Selection(
        [
            ("queued", "Queued at Telnyx"),
            ("sending", "Sending"),
            ("sent", "Sent"),
            ("delivered", "Delivered"),
            ("delivery_failed", "Delivery Failed"),
            ("sending_failed", "Sending Failed"),
            ("received", "Received"),
            ("error", "Error"),
        ],
        index=True,
    )
    error_code = fields.Char()
    error_message = fields.Char()
    raw_request = fields.Text(help="JSON payload sent to Telnyx (outbound only)")
    raw_response = fields.Text(help="JSON returned by Telnyx or received via webhook")
    sent_at = fields.Datetime()
    delivered_at = fields.Datetime()

    @api.model
    def log_outbound(self, sms, response_payload, error=None):
        """Record an outbound attempt. ``sms`` is an sms.sms recordset (single)."""
        vals = {
            "direction": "outbound",
            "sms_sms_id": sms.id,
            "partner_id": sms.partner_id.id if sms.partner_id else False,
            "to_number": sms.number,
            "body": sms.body,
        }
        if error:
            vals.update({
                "state": "error",
                "error_message": str(error)[:500],
                "raw_response": str(error)[:4000],
            })
        elif response_payload:
            data = (response_payload or {}).get("data") or {}
            vals.update({
                "telnyx_message_id": data.get("id"),
                "from_number": (data.get("from") or {}).get("phone_number"),
                "state": "queued",
                "sent_at": fields.Datetime.now(),
                "raw_response": str(response_payload)[:4000],
            })
        return self.create(vals)
