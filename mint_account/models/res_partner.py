# -*- coding: utf-8 -*-
from odoo import fields, models

# Where a customer/staff granted (or revoked) consent. Stored as varchar —
# the column is pre-created in fix-config.sh (Phase 1); keep types in sync.
_OPT_IN_SOURCES = [
    ("internal_staff", "Internal Staff"),
    ("external_web", "Customer Web Form"),
    ("manual", "Manual"),
]


class ResPartner(models.Model):
    _inherit = "res.partner"

    # ── Email marketing consent (consent-first: OFF by default) ──────────
    # Mirrors the SMS opt-in shape in mint_sms_telnyx/models/res_partner.py.
    email_opt_in = fields.Boolean(
        string="Email Opt-In",
        help="Explicit consent to receive marketing / discretionary email. "
             "OFF by default. Transactional & security email (password reset, "
             "order-ready, verification) is EXEMPT and always sends. Set via "
             "the Astro account profile (external_web) or a staff toggle.",
        index=True,
    )
    email_opt_in_date = fields.Datetime(
        string="Email Consent Date", readonly=True,
        help="Timestamp of the last email opt-in/opt-out action.",
    )
    email_opt_in_source = fields.Selection(
        _OPT_IN_SOURCES, string="Email Opt-In Source", readonly=True,
    )

    # ── Phone-call consent (no programmatic call channel exists yet) ─────
    call_opt_in = fields.Boolean(
        string="Call Opt-In",
        help="Explicit consent to receive phone calls. OFF by default.",
        index=True,
    )
    call_opt_in_date = fields.Datetime(
        string="Call Consent Date", readonly=True,
        help="Timestamp of the last call opt-in/opt-out action.",
    )
    call_opt_in_source = fields.Selection(
        _OPT_IN_SOURCES, string="Call Opt-In Source", readonly=True,
    )

    # ── Helpers — single place that mutates consent state ────────────────
    def set_email_opt_in(self, source="manual"):
        """Grant email marketing consent."""
        self.write({
            "email_opt_in": True,
            "email_opt_in_date": fields.Datetime.now(),
            "email_opt_in_source": source,
        })
        return True

    def set_email_opt_out(self):
        """Revoke email marketing consent. Source retained for audit."""
        self.write({
            "email_opt_in": False,
            "email_opt_in_date": fields.Datetime.now(),
        })
        return True

    def set_call_opt_in(self, source="manual"):
        """Grant phone-call consent."""
        self.write({
            "call_opt_in": True,
            "call_opt_in_date": fields.Datetime.now(),
            "call_opt_in_source": source,
        })
        return True

    def set_call_opt_out(self):
        """Revoke phone-call consent. Source retained for audit."""
        self.write({
            "call_opt_in": False,
            "call_opt_in_date": fields.Datetime.now(),
        })
        return True

    # ── Consent-first send predicate ─────────────────────────────────────
    def _marketing_email_allowed(self):
        """True only when this partner has explicitly opted in to marketing
        email. Transactional / security email must NOT use this gate — it
        always sends. Phase 3 wires this into the mass_mailing recipient
        filter + welcome/marketing templates once SMTP (#86555) is restored.
        """
        self.ensure_one()
        return bool(self.email_opt_in)
