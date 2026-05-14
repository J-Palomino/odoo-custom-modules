import json
import logging
import threading

import requests

from odoo import api, models

_logger = logging.getLogger(__name__)


class MailThread(models.AbstractModel):
    """Extend mail.thread to fire webhook events on create/write/unlink.

    Only fires for models listed in the daisydo_webhook.models config
    parameter (or all models when set to "*").  The HTTP POST runs in a
    daemon thread so it never blocks or breaks the Odoo transaction.
    """
    _inherit = "mail.thread"

    def _webhook_enabled(self):
        """Check if webhooks are enabled and this model is in the whitelist."""
        ICP = self.env["ir.config_parameter"].sudo()
        if ICP.get_param("daisydo_webhook.enabled", "0") != "1":
            return False
        models_raw = ICP.get_param("daisydo_webhook.models", "*")
        if models_raw == "*":
            return True
        try:
            allowed = json.loads(models_raw)
            return self._name in allowed
        except (json.JSONDecodeError, TypeError):
            return False

    def _fire_webhook(self, event, record_ids, data=None):
        """POST event to FastAPI in a background thread."""
        ICP = self.env["ir.config_parameter"].sudo()
        base_url = ICP.get_param("daisydo_webhook.fastapi_url", "http://fastapi:8000")
        secret = ICP.get_param("daisydo_webhook.event_secret", "")

        user_id = self.env.uid
        user_name = self.env.user.name if self.env.user else ""

        payload = {
            "model": self._name,
            "event": event,
            "record_ids": record_ids,
            "data": data or {},
            "odoo_user_id": user_id,
            "odoo_user_name": user_name,
        }

        def _post():
            try:
                headers = {"Content-Type": "application/json"}
                if secret:
                    headers["X-Webhook-Secret"] = secret
                resp = requests.post(
                    f"{base_url}/api/webhooks/events",
                    json=payload,
                    headers=headers,
                    timeout=5,
                )
                if resp.status_code >= 400:
                    _logger.warning(
                        "Webhook POST failed (%s %s): %s",
                        resp.status_code, self._name, resp.text[:200],
                    )
            except Exception:
                _logger.warning("Webhook POST error for %s", self._name, exc_info=True)

        t = threading.Thread(target=_post, daemon=True)
        t.start()

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        if self._webhook_enabled():
            # Collect basic data from the first few created records
            data = {}
            for rec in records[:5]:
                if hasattr(rec, "name"):
                    data["name"] = rec.name
                    break
            self._fire_webhook("create", records.ids, data)
        return records

    def write(self, vals):
        result = super().write(vals)
        if self._webhook_enabled():
            self._fire_webhook("write", self.ids, {"fields": list(vals.keys())})
        return result

    def unlink(self):
        ids = self.ids
        result = super().unlink()
        if self._webhook_enabled():
            self._fire_webhook("unlink", ids)
        return result
