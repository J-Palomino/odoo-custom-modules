# -*- coding: utf-8 -*-
from odoo import fields, models

# One source list shared by the master opt-in and both category consents.
CONSENT_SOURCES = [
    ("internal_staff", "Internal Staff"),
    ("external_web", "Customer Web Form"),
    ("sms_keyword", "SMS Keyword (START/YES)"),
    ("manual", "Manual"),
]

SMS_CONSENT_CATEGORIES = ("transactional", "marketing")


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
        return True

    def clear_sms_consent(self, category):
        """Revoke one category of consent. Last grant date/source are kept
        as history (mirrors how set_sms_opt_out keeps sms_opt_in_date).
        The master opt-in is untouched — the other category may still stand.
        """
        if category not in SMS_CONSENT_CATEGORIES:
            raise ValueError("Unknown SMS consent category: %r" % (category,))
        self.write({"sms_consent_%s" % category: False})
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
        """
        self.write({
            "sms_opt_out": True,
            "sms_opt_out_date": fields.Datetime.now(),
            "sms_opt_in": False,
            "sms_consent_transactional": False,
            "sms_consent_marketing": False,
        })
        return True

    # UI buttons (internal staff opt-in flow)
    def action_sms_opt_in(self):
        return self.set_sms_opt_in(source="internal_staff")

    def action_sms_opt_out(self):
        return self.set_sms_opt_out()
