# -*- coding: utf-8 -*-
from odoo import fields, models


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
        [
            ("internal_staff", "Internal Staff"),
            ("external_web", "Customer Web Form"),
            ("sms_keyword", "SMS Keyword (START/YES)"),
            ("manual", "Manual"),
        ],
        string="Opt-In Source",
        readonly=True,
    )

    # ------------------------------------------------------------------
    # Helpers — single place that mutates opt-in/out state + whitelist tag
    # ------------------------------------------------------------------
    def set_sms_opt_in(self, source="manual", add_whitelist=True):
        """Grant SMS consent: set opt-in, clear opt-out, add whitelist tag."""
        cat = self.env["mint.sms.message"]._whitelist_category()
        vals = {
            "sms_opt_in": True,
            "sms_opt_in_date": fields.Datetime.now(),
            "sms_opt_in_source": source,
            "sms_opt_out": False,
            "sms_opt_out_date": False,
        }
        if add_whitelist and cat:
            vals["category_id"] = [(4, cat.id)]
        self.write(vals)
        return True

    def set_sms_opt_out(self):
        """Revoke consent: set opt-out, clear opt-in. Whitelist tag retained."""
        self.write({
            "sms_opt_out": True,
            "sms_opt_out_date": fields.Datetime.now(),
            "sms_opt_in": False,
        })
        return True

    # UI buttons (internal staff opt-in flow)
    def action_sms_opt_in(self):
        return self.set_sms_opt_in(source="internal_staff")

    def action_sms_opt_out(self):
        return self.set_sms_opt_out()
