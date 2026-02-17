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
        return info
