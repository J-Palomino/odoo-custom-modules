from odoo import api, models

DEFAULT_API_URL = (
    "https://daisy.plus/api/v1/prediction/af2db2cc-f48c-4ca8-b1a9-36b156ec06cf"
)


class DaisyErrorHandlerConfig(models.AbstractModel):
    """Sudo-backed accessor for the error handler's config parameters.

    The backend JS (static/src/js/error_handler.js) used to read
    ir.config_parameter directly as the logged-in user, which raises
    AccessError for every non-admin and silently falls back to hardcoded
    defaults — so the admin kill-switch never worked for regular users
    (maintenance request #522).
    """

    _name = "daisy.error.handler.config"
    _description = "Daisy Error Handler Config"

    @api.model
    def get_config(self):
        # Param values stay inside the internal perimeter: portal/public
        # users get the hardcoded defaults instead of a sudo read.
        if not self.env.user._is_internal():
            return {"api_url": DEFAULT_API_URL, "enabled": True}
        icp = self.env["ir.config_parameter"].sudo()
        api_url = icp.get_param("daisy.error_handler_api_url", "") or DEFAULT_API_URL
        enabled = icp.get_param("daisy.error_handler_enabled", "true")
        return {
            "api_url": api_url,
            "enabled": str(enabled).strip().lower() != "false",
        }
