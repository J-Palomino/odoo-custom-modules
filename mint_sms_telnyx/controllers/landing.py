# -*- coding: utf-8 -*-
"""
Public SMS notifications landing page at /sms.

Standalone HTML — no Odoo website chrome — so TFV reviewers see ONLY the
opt-in consent, terms, and privacy content with the "Mint" brand. This
is the URL submitted on the Telnyx Toll-Free Verification application.
"""
from odoo import http
from odoo.http import request


SMS_LANDING_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
    <meta name="robots" content="noindex"/>
    <title>Mint — SMS Notifications</title>
    <style>
        :root {
            --ink: #1a1a1a;
            --muted: #555;
            --line: #e3e3e3;
            --accent: #0a7a4f;
            --bg: #ffffff;
            --soft: #f7f7f5;
        }
        * { box-sizing: border-box; }
        html, body { margin: 0; padding: 0; background: var(--bg); color: var(--ink); }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                         "Helvetica Neue", Arial, sans-serif;
            font-size: 16px;
            line-height: 1.6;
        }
        .wrap { max-width: 760px; margin: 0 auto; padding: 48px 24px 80px; }
        header { border-bottom: 1px solid var(--line); padding-bottom: 24px; margin-bottom: 32px; }
        .brand { font-size: 28px; font-weight: 700; letter-spacing: -0.5px; margin: 0; }
        .subtitle { color: var(--muted); margin: 6px 0 0; font-size: 15px; }
        h2 { font-size: 18px; margin: 32px 0 10px; letter-spacing: -0.2px; }
        p { margin: 0 0 12px; }
        ul { margin: 0 0 16px; padding-left: 22px; }
        li { margin-bottom: 6px; }
        .consent {
            background: var(--soft);
            border-left: 3px solid var(--accent);
            padding: 16px 20px;
            margin: 8px 0 20px;
            border-radius: 4px;
        }
        .samples {
            list-style: none;
            padding: 0;
            margin: 0 0 16px;
        }
        .samples li {
            background: var(--soft);
            border-radius: 4px;
            padding: 10px 14px;
            margin-bottom: 8px;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, monospace;
            font-size: 14px;
            color: #2a2a2a;
        }
        .keyword { font-weight: 700; color: var(--accent); }
        a { color: var(--accent); }
        footer {
            border-top: 1px solid var(--line);
            margin-top: 48px;
            padding-top: 20px;
            color: var(--muted);
            font-size: 13px;
        }
        footer p { margin: 0 0 6px; }
    </style>
</head>
<body>
    <main class="wrap">
        <header>
            <h1 class="brand">Mint</h1>
            <p class="subtitle">SMS Notifications &amp; Account Alerts</p>
        </header>

        <h2>What you&apos;ll receive</h2>
        <p>By opting in, you&apos;ll receive transactional text messages from Mint about your orders and account. These include:</p>
        <ul>
            <li>Order ready notifications</li>
            <li>Pickup window reminders</li>
            <li>Verification codes (one-time passcodes)</li>
            <li>Account-related alerts</li>
        </ul>

        <h2>Sample messages</h2>
        <ul class="samples">
            <li>Mint: Your order #1234 is ready for pickup. We&apos;re open until 9pm tonight.</li>
            <li>Mint: Your pickup window starts at 4pm today. Reply STOP to opt out.</li>
            <li>Mint: Your verification code is 482917. Expires in 5 minutes.</li>
        </ul>

        <h2>How you opt in</h2>
        <p>You may provide your mobile number and consent to SMS at checkout (online or in-store), or by adding your number to your account profile and explicitly opting in. Consent is captured with the language shown below and recorded with the timestamp and source.</p>

        <h2>Consent</h2>
        <div class="consent">
            By providing your phone number, you agree to receive transactional SMS from Mint regarding your orders. Message frequency varies. Msg &amp; data rates may apply. Reply <span class="keyword">STOP</span> to opt out, <span class="keyword">HELP</span> for help.
        </div>

        <h2>Opt out anytime</h2>
        <p>Reply <span class="keyword">STOP</span> to any message to unsubscribe. You will receive a single confirmation message and no further texts. To resubscribe, reply <span class="keyword">START</span>.</p>
        <p>Reply <span class="keyword">HELP</span> for assistance, or email <a href="mailto:support@letsgomint.us">support@letsgomint.us</a>.</p>

        <h2>SMS Privacy</h2>
        <p>Mint does not sell, rent, lease, or share mobile phone numbers or SMS opt-in data with any third party for marketing or promotional purposes. Phone numbers collected for SMS are used solely to send the transactional messages described above.</p>
        <p>Number information is shared only with our SMS service provider (Telnyx) for the purpose of delivering messages, and as required by law or carrier compliance.</p>

        <h2>SMS Terms</h2>
        <p>By opting in, you agree to receive transactional SMS messages from Mint. Messages are sent only to confirm orders, deliver pickup notifications, and provide verification codes. Message frequency varies based on your activity. Standard message and data rates from your carrier may apply.</p>
        <p>Supported carriers include AT&amp;T, Verizon, T-Mobile, US Cellular, and most regional carriers. Carriers are not liable for delayed or undelivered messages.</p>
        <p>To opt out, reply STOP to any message. For help, reply HELP or contact <a href="mailto:support@letsgomint.us">support@letsgomint.us</a>.</p>

        <footer>
            <p>Operated by <strong>Cerberean Group LLC</strong>. &ldquo;Mint&rdquo; is a registered DBA of Cerberean Group LLC.</p>
            <p>Support: <a href="mailto:support@letsgomint.us">support@letsgomint.us</a></p>
        </footer>
    </main>
</body>
</html>"""


class SmsLanding(http.Controller):

    @http.route("/sms", type="http", auth="public", methods=["GET"], csrf=False)
    def sms_landing(self, **kw):
        return request.make_response(
            SMS_LANDING_HTML,
            headers=[
                ("Content-Type", "text/html; charset=utf-8"),
                ("X-Robots-Tag", "noindex"),
                ("Cache-Control", "public, max-age=300"),
            ],
        )
