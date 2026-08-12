# -*- coding: utf-8 -*-
{
    'name': 'Mint SMS — Telnyx Gateway (Transactional Only)',
    'version': '19.0.4.3.0',
    'category': 'Tools',
    'summary': 'Transactional SMS via Telnyx + gated iMessage (BlueBubbles) — cannabis-content guardrail',
    'description': """
        Routes Odoo SMS through Telnyx for TRANSACTIONAL messages only
        (order ready, pickup window, OTP, appointment reminders).

        Marketing/promotional SMS for cannabis content is prohibited by
        US carrier AUPs (SHAFT+C). This module enforces that by scanning
        outbound bodies for cannabis terminology and refusing to send.

        Features:
          - Outbound: overrides sms.sms._send() to POST to Telnyx /v2/messages
          - Content scanner: blocks cannabis terms by default; tunable blocklist
          - Refuses sms.sms records originating from mass_mailing_sms (configurable)
          - Delivery receipts: webhook updates sms.sms.state in real time
          - Inbound: routes incoming SMS to matched res.partner chatter
                    (falls back to a configurable Discuss channel)
          - STOP/UNSTOP keyword handling sets res.partner.sms_opt_out
          - Per-message audit log (telnyx.message.log) for support/debug
          - Webhook Ed25519 signature verification

        Configuration: Settings > General Settings > SMS > Telnyx.
        Webhook URL: {base_url}/telnyx/sms/webhook
    """,
    'author': 'Mint Dispensaries',
    'website': 'https://mintdispensaries.com',
    'license': 'LGPL-3',
    'depends': [
        'base',
        'mail',
        'sms',
        'phone_validation',
    ],
    'external_dependencies': {
        'python': ['nacl'],  # PyNaCl for Ed25519 signature verification
    },
    'data': [
        'security/sms_security.xml',
        'security/ir.model.access.csv',
        'data/ir_config_parameter.xml',
        'views/res_config_settings_views.xml',
        'views/telnyx_message_log_views.xml',
        'views/mint_sms_message_views.xml',
        'views/res_partner_views.xml',
    ],
    'installable': True,
    'auto_install': False,
    'application': False,
}
