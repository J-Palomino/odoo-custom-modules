# -*- coding: utf-8 -*-
"""
Public SMS notifications landing page at /sms.

Standalone HTML — no Odoo website chrome — so TFV reviewers see ONLY the
opt-in consent, terms, and privacy content with the "Mint" brand. This
is the URL submitted on the Telnyx Toll-Free Verification application.
"""
import json
import re

from odoo import http
from odoo.http import request, Response


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
        .optin {
            background: var(--soft);
            border: 1px solid var(--line);
            border-radius: 8px;
            padding: 20px;
            margin: 8px 0 20px;
        }
        .optin label { display: block; font-size: 14px; font-weight: 600; margin: 12px 0 4px; }
        .optin input[type="text"], .optin input[type="tel"] {
            width: 100%; padding: 10px 12px; border: 1px solid var(--line);
            border-radius: 6px; font-size: 16px;
        }
        .optin .check { display: flex; gap: 10px; align-items: flex-start; margin-top: 16px; font-weight: 400; font-size: 14px; }
        .optin .check input { margin-top: 3px; }
        .optin button {
            margin-top: 16px; background: var(--accent); color: #fff; border: 0;
            border-radius: 6px; padding: 12px 20px; font-size: 16px; font-weight: 600; cursor: pointer;
        }
        .optin button:disabled { opacity: 0.6; cursor: not-allowed; }
        .optin .status { margin-top: 14px; font-size: 14px; min-height: 20px; }
        .optin .status.ok { color: var(--accent); font-weight: 600; }
        .optin .status.err { color: #b00020; }
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
        <p>You may provide your mobile number and consent to SMS at checkout (online or in-store), by adding your number to your account profile, or using the form below. Consent is captured with the language shown below and recorded with the timestamp and source.</p>

        <h2>Opt in now</h2>
        <form class="optin" id="sms-optin-form" novalidate>
            <label for="optin-name">Name (optional)</label>
            <input type="text" id="optin-name" name="fullName" autocomplete="name"/>
            <label for="optin-phone">Mobile number</label>
            <input type="tel" id="optin-phone" name="phone" autocomplete="tel" inputmode="tel" placeholder="(480) 555-0123" required/>
            <label class="check">
                <input type="checkbox" id="optin-consent" name="consent" required/>
                <span>By checking this box, I agree to receive transactional SMS from Mint regarding my orders. Message frequency varies. Msg &amp; data rates may apply. Reply STOP to opt out, HELP for help.</span>
            </label>
            <button type="submit" id="optin-submit">Opt in to texts</button>
            <div class="status" id="optin-status" role="status" aria-live="polite"></div>
        </form>

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
    <script>
        (function () {
            var form = document.getElementById('sms-optin-form');
            if (!form) return;
            var statusEl = document.getElementById('optin-status');
            var btn = document.getElementById('optin-submit');
            form.addEventListener('submit', async function (e) {
                e.preventDefault();
                statusEl.className = 'status'; statusEl.textContent = '';
                var phone = document.getElementById('optin-phone').value.trim();
                var consent = document.getElementById('optin-consent').checked;
                if (!phone) { statusEl.className = 'status err'; statusEl.textContent = 'Please enter your mobile number.'; return; }
                if (!consent) { statusEl.className = 'status err'; statusEl.textContent = 'Please check the consent box to opt in.'; return; }
                btn.disabled = true; statusEl.textContent = 'Submitting…';
                try {
                    var res = await fetch('/sms/opt-in', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            fullName: document.getElementById('optin-name').value.trim(),
                            phone: phone, consent: consent
                        })
                    });
                    var data = await res.json().catch(function () { return {}; });
                    if (res.ok && data.success) {
                        form.reset();
                        statusEl.className = 'status ok';
                        statusEl.textContent = "You're opted in. You'll receive transactional texts from Mint. Reply STOP anytime to opt out.";
                    } else {
                        statusEl.className = 'status err';
                        statusEl.textContent = (data && data.error) || 'Something went wrong. Please try again.';
                    }
                } catch (err) {
                    statusEl.className = 'status err';
                    statusEl.textContent = 'Network error. Please try again.';
                } finally { btn.disabled = false; }
            });
        })();
    </script>
</body>
</html>"""


def _normalize_e164(raw):
    """10 digits -> +1XXXXXXXXXX (matches res.partner.phone_sanitized)."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return None
    return "+1" + digits


class SmsLanding(http.Controller):

    def _json(self, payload, status=200):
        return Response(json.dumps(payload), status=status,
                        content_type="application/json")

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

    @http.route("/sms/opt-in", type="http", auth="public", methods=["POST"],
                csrf=False, save_session=False)
    def sms_opt_in(self, **kw):
        """Customer SMS consent capture from the /sms page (same-origin)."""
        try:
            body = json.loads(request.httprequest.get_data(as_text=True) or "{}")
        except ValueError:
            return self._json({"success": False, "error": "Invalid request."}, 400)

        if body.get("consent") not in (True, "true"):
            return self._json(
                {"success": False,
                 "error": "You must agree to receive texts to opt in."}, 400)

        number = _normalize_e164(body.get("phone"))
        if not number:
            return self._json(
                {"success": False, "error": "Enter a valid mobile number."}, 400)

        name = (body.get("fullName") or "").strip()[:120]
        Partner = request.env["res.partner"].sudo()
        partner = Partner.search([("phone_sanitized", "=", number)], limit=1)
        if not partner:
            partner = Partner.create({"name": name or ("SMS Opt-In " + number),
                                      "phone": number})
        # Sets opt-in + timestamp + source, clears opt-out, adds whitelist tag.
        partner.set_sms_opt_in(source="external_web")
        return self._json({"success": True})
