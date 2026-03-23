from odoo import models, fields, api


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    # --- Daisy+ AI ---
    daisy_api_base_url = fields.Char(
        string="Daisy+ API Base URL",
        config_parameter="daisy.api_base_url",
        default="https://daisy.plus/api/v1",
    )
    daisy_global_api_key = fields.Char(
        string="Daisy+ Global API Key",
        config_parameter="daisy.global_api_key",
        help="Shared API key used when creating new agents. "
             "If set, admins won't need to enter it each time.",
    )
    daisy_template_chatflow_id = fields.Char(
        string="Template Chatflow ID",
        config_parameter="daisy.template_chatflow_id",
        help="Chatflow to clone when creating new agents. "
             "This should be a fully configured agentflow with MCP tool node. "
             "New agents get a copy with their own Odoo credentials injected.",
    )

    # --- Go2RTC Media Server ---
    go2rtc_api_url = fields.Char(
        string="Go2RTC Internal URL",
        help="Railway-internal URL used by Odoo server to reach Go2RTC REST API",
        config_parameter="daisy.go2rtc_api_url",
        default="http://go2rtc-media.railway.internal:1984",
    )
    go2rtc_public_url = fields.Char(
        string="Go2RTC Public URL",
        help="Public URL used by browsers for WebSocket/WebRTC connections",
        config_parameter="daisy.go2rtc_public_url",
        default="https://go2rtc-media-production.up.railway.app",
    )
