# -*- coding: utf-8 -*-
import logging
import re

import requests

from odoo import fields, models

_logger = logging.getLogger(__name__)

# One source list shared by the master opt-in and both category consents.
CONSENT_SOURCES = [
    ("internal_staff", "Internal Staff"),
    ("external_web", "Customer Web Form"),
    ("sms_keyword", "SMS Keyword (START/YES)"),
    ("manual", "Manual"),
]

SMS_CONSENT_CATEGORIES = ("transactional", "marketing")

# Confirmation texted on a fresh grant of each category (MR-1250). Sent in
# the granted category so it self-tests the exact permission just given.
# Wording must stay clear of the sms.sms content guardrail's term blocklist
# (e.g. "cannabis" blocks — learned live on message #38).
CONSENT_CONFIRM_BODIES = {
    "transactional": "Mint: You're set to receive order & account texts. "
                     "Reply STOP to opt out.",
    "marketing": "Mint: You're in! You'll get occasional deal texts from "
                 "Mint. Reply STOP to opt out.",
}

# The single unsubscribe receipt the /sms landing page promises ("You will
# receive a single confirmation message and no further texts"). Same
# guardrail constraint as the bodies above: no blocklisted terms.
OPTOUT_CONFIRM_BODY = (
    "Mint: You're unsubscribed and will receive no further texts. "
    "Reply START to resubscribe."
)


class ResPartner(models.Model):
    _inherit = "res.partner"

    sms_opt_out = fields.Boolean(
        string="SMS Opt-Out",
        help="Set automatically when this partner texts STOP. "
             "Cleared on UNSTOP/START. Outbound SMS skips opt-out partners.",
        index=True,
    )
    sms_opt_out_date = fields.Datetime(readonly=True)

    # ------------------------------------------------------------------
    # Opt-in (explicit consent — required by the iMessage send gate)
    # ------------------------------------------------------------------
    sms_opt_in = fields.Boolean(
        string="SMS Opt-In",
        help="Explicit consent to receive texts. Required before any send. "
             "Set via internal staff toggle, the customer web form, or a "
             "START/YES keyword reply.",
        index=True,
    )
    sms_opt_in_date = fields.Datetime(readonly=True)
    sms_opt_in_source = fields.Selection(
        CONSENT_SOURCES,
        string="Opt-In Source",
        readonly=True,
    )

    # ------------------------------------------------------------------
    # Per-category consent (MR-1250). The master opt-in above stays the
    # umbrella switch; these refine WHICH texts the partner agreed to.
    # The send gate additionally requires the category matching the message.
    # ------------------------------------------------------------------
    sms_consent_transactional = fields.Boolean(
        string="Transactional Texts",
        help="Consent to order/account texts. Legacy single opt-ins were "
             "migrated to this category only (never to marketing).",
        index=True,
    )
    sms_consent_transactional_date = fields.Datetime(readonly=True)
    sms_consent_transactional_source = fields.Selection(
        CONSENT_SOURCES,
        string="Transactional Source",
        readonly=True,
    )
    sms_consent_marketing = fields.Boolean(
        string="Marketing Texts",
        help="Consent to promotional texts. Always requires a fresh explicit "
             "grant — never migrated from the legacy single opt-in.",
        index=True,
    )
    sms_consent_marketing_date = fields.Datetime(readonly=True)
    sms_consent_marketing_source = fields.Selection(
        CONSENT_SOURCES,
        string="Marketing Source",
        readonly=True,
    )

    # ------------------------------------------------------------------
    # Helpers — single place that mutates opt-in/out state + whitelist tag
    # ------------------------------------------------------------------
    def set_sms_consent(self, category, source="manual", add_whitelist=True):
        """Grant one category of SMS consent.

        Granting ANY category also flips the master opt-in (the gate needs
        both) and clears a prior opt-out — opting in supersedes STOP.
        """
        if category not in SMS_CONSENT_CATEGORIES:
            raise ValueError("Unknown SMS consent category: %r" % (category,))
        cat = self.env["mint.sms.message"]._whitelist_category()
        now = fields.Datetime.now()
        # Grant TRANSITIONS only (False -> True), captured before the write:
        # re-saves and repeated PUTs must not re-text the confirmation.
        newly_granted = self.filtered(
            lambda p: not p["sms_consent_%s" % category])
        vals = {
            "sms_opt_in": True,
            "sms_opt_in_date": now,
            "sms_opt_in_source": source,
            "sms_opt_out": False,
            "sms_opt_out_date": False,
            "sms_consent_%s" % category: True,
            "sms_consent_%s_date" % category: now,
            "sms_consent_%s_source" % category: source,
        }
        if add_whitelist and cat:
            vals["category_id"] = [(4, cat.id)]
        self.write(vals)
        # Delivery-side gate: the proxy keeps its own whitelist; a grant
        # must land there too or sends die at the proxy (MR-1250).
        self._sync_proxy_whitelist("add")
        # Confirmation text AFTER the proxy add, so its own delivery path is
        # already open — arriving is the end-to-end proof of the fresh grant.
        newly_granted._send_consent_confirmation(category)
        return True

    def _send_consent_confirmation(self, category):
        """Text the customer that their consent is now active (MR-1250).

        Gated by ir.config_parameter mint_sms_telnyx.consent_confirm_enabled
        ('True' to enable) so environments never surprise-text. Best-effort:
        a failed/blocked confirmation never fails the consent write — the
        mint.sms.message row is the audit either way.
        """
        if not self:
            return
        enabled = self.env["ir.config_parameter"].sudo().get_param(
            "mint_sms_telnyx.consent_confirm_enabled")
        if enabled != "True":
            return
        Sms = self.env["mint.sms.message"].sudo()
        for partner in self:
            if not partner.phone_sanitized:
                continue
            try:
                rec = Sms.create({
                    "partner_id": partner.id,
                    "category": category,
                    "body": CONSENT_CONFIRM_BODIES[category],
                })
                _logger.info(
                    "Consent confirmation [%s] for partner %s -> %s%s",
                    category, partner.id, rec.state,
                    (" (%s)" % rec.block_reason) if rec.block_reason else "")
            except Exception:
                _logger.exception(
                    "Consent confirmation [%s] failed for partner %s",
                    category, partner.id)

    def clear_sms_consent(self, category):
        """Revoke one category of consent. Last grant date/source are kept
        as history (mirrors how set_sms_opt_out keeps sms_opt_in_date).
        The master opt-in is untouched — the other category may still stand.
        """
        if category not in SMS_CONSENT_CATEGORIES:
            raise ValueError("Unknown SMS consent category: %r" % (category,))
        self.write({"sms_consent_%s" % category: False})
        # Only de-whitelist at the proxy when NO category consent remains —
        # the other category may still be sendable.
        self.filtered(
            lambda p: not (p.sms_consent_transactional
                           or p.sms_consent_marketing)
        )._sync_proxy_whitelist("remove")
        return True

    def set_sms_opt_in(self, source="manual", add_whitelist=True):
        """Legacy single-consent entry point (/sms form, START keyword,
        staff button). Maps to TRANSACTIONAL consent only — the same mapping
        the 19.0.4.0.0 migration applied to historical opt-ins (MR-1250).
        Marketing always requires its own explicit set_sms_consent call.
        """
        return self.set_sms_consent(
            "transactional", source=source, add_whitelist=add_whitelist)

    def set_sms_opt_out(self):
        """Revoke consent entirely (STOP): set opt-out, clear the master
        opt-in AND both category consents — a full stop, not per-category.
        Whitelist tag retained.

        The goodbye text goes FIRST, and the ordering is load-bearing: once
        the flags below are written the send gate refuses this partner, and
        _sync_proxy_whitelist("remove") closes delivery at the proxy — so
        the single confirmation the /sms page promises ("You will receive a
        single confirmation message and no further texts") can only leave
        while consent still stands.
        """
        self._send_opt_out_confirmation()
        self.write({
            "sms_opt_out": True,
            "sms_opt_out_date": fields.Datetime.now(),
            "sms_opt_in": False,
            "sms_consent_transactional": False,
            "sms_consent_marketing": False,
        })
        self._sync_proxy_whitelist("remove")
        return True

    def _send_opt_out_confirmation(self):
        """Text the single unsubscribe confirmation promised on /sms.

        Gated by ir.config_parameter mint_sms_telnyx.optout_confirm_enabled
        ('True' to enable) — deliberately separate from
        consent_confirm_enabled so either direction can ship alone. Must be
        called BEFORE the opt-out flags are written (see set_sms_opt_out).
        Best-effort: a failed/blocked goodbye never fails the revocation —
        the mint.sms.message row is the audit either way.

        Skipped for partners who are already opted out (a repeated STOP gets
        one confirmation, not one per STOP) and for partners holding no
        category consent — they could never receive texts, so no
        confirmation is owed, and the category gate would block it anyway.
        """
        enabled = self.env["ir.config_parameter"].sudo().get_param(
            "mint_sms_telnyx.optout_confirm_enabled")
        if enabled != "True":
            return
        Sms = self.env["mint.sms.message"].sudo()
        for partner in self:
            if partner.sms_opt_out or not partner.phone_sanitized:
                continue
            # Send in a category the partner actually holds, or the gate
            # refuses it. Transactional preferred: an unsubscribe receipt is
            # service messaging, not marketing.
            category = (
                "transactional" if partner.sms_consent_transactional
                else "marketing" if partner.sms_consent_marketing
                else None)
            if not category:
                continue
            try:
                rec = Sms.create({
                    "partner_id": partner.id,
                    "category": category,
                    "body": OPTOUT_CONFIRM_BODY,
                })
                _logger.info(
                    "Opt-out confirmation for partner %s -> %s%s",
                    partner.id, rec.state,
                    (" (%s)" % rec.block_reason) if rec.block_reason else "")
            except Exception:
                _logger.exception(
                    "Opt-out confirmation failed for partner %s", partner.id)

    def _sms_e164(self):
        """Best-effort E.164 destination number for SMS delivery.

        phone_sanitized when Odoo computed one; otherwise derived from the
        raw phone assuming NANP. Web signups store bare 10-digit numbers on
        partners with no country_id, which leaves phone_sanitized empty —
        that made consented customers invisible to the proxy whitelist sync
        and undeliverable by the automatic points text (both observed on
        prod 2026-08-12, partner 2743079).
        """
        self.ensure_one()
        if self.phone_sanitized:
            return self.phone_sanitized
        digits = re.sub(r"\D", "", self.phone or "")
        if len(digits) == 10:
            return "+1" + digits
        if len(digits) == 11 and digits.startswith("1"):
            return "+" + digits
        return ""

    def _sync_proxy_whitelist(self, action):
        """Mirror consent to the BlueBubbles proxy's own delivery whitelist.

        The proxy in front of BlueBubbles keeps a SECOND whitelist and 403s
        sends to numbers not on it — so Odoo-side consent alone leaves texts
        silently failing at delivery (observed 2026-08-11: a fully opted-in,
        Odoo-whitelisted customer got HTTP 403 until her number was added at
        the proxy by hand). Mirroring here makes the client opt-in flow
        end-to-end: consent recorded AND deliverable in one step.

        Strictly best-effort, and called only AFTER the consent write: the
        proxy lives behind a free-ngrok tunnel that comes and goes, and a
        delivery-gate hiccup must never fail or roll back the consent
        record. Failures are logged; the number can be replayed by calling
        this method again (add/remove are idempotent on the proxy —
        "changed" in its response says whether anything moved).
        """
        icp = self.env["ir.config_parameter"].sudo()
        base = (icp.get_param("mint_sms_telnyx.bluebubbles_url") or "").rstrip("/")
        token = icp.get_param("mint_sms_telnyx.proxy_whitelist_token")
        if not base or not token:
            _logger.warning(
                "Proxy whitelist %s skipped: bluebubbles_url or "
                "proxy_whitelist_token not configured", action)
            return
        for partner in self:
            number = partner._sms_e164()
            if not number:
                _logger.warning(
                    "Proxy whitelist %s skipped for partner %s: no usable "
                    "phone (raw %r)", action, partner.id, partner.phone)
                continue
            try:
                resp = requests.post(
                    base + "/admin/whitelist",
                    json={"number": number, "action": action},
                    headers={
                        "Authorization": "Bearer %s" % token,
                        "ngrok-skip-browser-warning": "true",
                    },
                    timeout=10,
                )
                if resp.ok:
                    _logger.info(
                        "Proxy whitelist %s for partner %s (%s): %s",
                        action, partner.id, number, resp.text[:200])
                else:
                    _logger.warning(
                        "Proxy whitelist %s FAILED for partner %s (%s): "
                        "HTTP %s %s", action, partner.id, number,
                        resp.status_code, resp.text[:200])
            except Exception:
                _logger.exception(
                    "Proxy whitelist %s errored for partner %s (%s)",
                    action, partner.id, number)

    # UI buttons (internal staff opt-in flow)
    def action_sms_opt_in(self):
        return self.set_sms_opt_in(source="internal_staff")

    def action_sms_opt_out(self):
        return self.set_sms_opt_out()
