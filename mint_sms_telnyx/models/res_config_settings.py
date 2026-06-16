# -*- coding: utf-8 -*-
from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    telnyx_enabled = fields.Boolean(
        string="Route SMS through Telnyx",
        config_parameter="mint_sms_telnyx.enabled",
        help="When enabled, sms.sms records send via Telnyx instead of Odoo IAP.",
    )
    telnyx_api_key = fields.Char(
        string="Telnyx API Key",
        config_parameter="mint_sms_telnyx.api_key",
        help="Bearer token from Telnyx Portal → API Keys.",
    )
    telnyx_messaging_profile_id = fields.Char(
        string="Messaging Profile ID",
        config_parameter="mint_sms_telnyx.messaging_profile_id",
        help="Telnyx Messaging Profile that owns the sending number(s).",
    )
    telnyx_from_number = fields.Char(
        string="From Number / Sender ID",
        config_parameter="mint_sms_telnyx.from_number",
        help="E.164 number (e.g. +14805551212) or alphanumeric sender ID.",
    )
    telnyx_webhook_public_key = fields.Char(
        string="Webhook Public Key",
        config_parameter="mint_sms_telnyx.webhook_public_key",
        help="Telnyx Portal → Messaging Profile → Webhook Public Key (Ed25519, base64).",
    )
    telnyx_inbound_channel_id = fields.Many2one(
        "discuss.channel",
        string="Inbound Fallback Channel",
        config_parameter="mint_sms_telnyx.inbound_channel_id",
        help="Discuss channel to post inbound SMS when no partner matches by phone.",
    )
    telnyx_api_base = fields.Char(
        string="API Base URL",
        config_parameter="mint_sms_telnyx.api_base",
        default="https://api.telnyx.com/v2",
    )
    telnyx_scan_mode = fields.Selection(
        [
            ("block", "Block on cannabis terms (recommended)"),
            ("warn", "Warn only — log but allow"),
            ("off", "Off — no scanning"),
        ],
        string="Content Scan Mode",
        config_parameter="mint_sms_telnyx.scan_mode",
        default="block",
        help="Transactional-only gateway. 'Block' is the safe default.",
    )
    telnyx_content_blocklist = fields.Char(
        string="Content Blocklist (comma-separated)",
        config_parameter="mint_sms_telnyx.content_blocklist",
        help="Substring matches, case-insensitive. Leave blank to use the "
             "module's built-in cannabis-term defaults.",
    )
    telnyx_block_mailing_sms = fields.Boolean(
        string="Refuse Mailing Campaigns",
        config_parameter="mint_sms_telnyx.block_mailing_sms",
        default=True,
        help="When enabled, sms.sms records originating from a mass_mailing_sms "
             "campaign are refused. Keep ON — this gateway is transactional-only.",
    )

    # ------------------------------------------------------------------
    # BlueBubbles iMessage transport (mint.sms.message)
    # ------------------------------------------------------------------
    bluebubbles_enabled = fields.Boolean(
        string="Enable iMessage (BlueBubbles)",
        config_parameter="mint_sms_telnyx.bluebubbles_enabled",
        help="When enabled, mint.sms.message records deliver via the BlueBubbles "
             "iMessage gateway. Same content guardrail + whitelist gate apply.",
    )
    bluebubbles_url = fields.Char(
        string="BlueBubbles URL",
        config_parameter="mint_sms_telnyx.bluebubbles_url",
        help="Base URL of the BlueBubbles server, e.g. the public ngrok endpoint.",
    )
    bluebubbles_password = fields.Char(
        string="BlueBubbles Password",
        config_parameter="mint_sms_telnyx.bluebubbles_password",
        help="Server password — sent as ?password= on every request.",
    )
    bluebubbles_whitelist_category_id = fields.Many2one(
        "res.partner.category",
        string="SMS Whitelist Tag",
        config_parameter="mint_sms_telnyx.whitelist_category_id",
        help="Only contacts carrying this tag may receive texts. Defaults to the "
             "'SMS Whitelist' category when left blank.",
    )
