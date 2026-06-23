# -*- coding: utf-8 -*-
from odoo import models, fields, api


# Mirror of the SMS opt-in source list (mint_sms_telnyx), minus the SMS-only
# 'sms_keyword'. external_web = set by the customer via the Astro account profile.
OPT_IN_SOURCES = [
    ('internal_staff', 'Internal Staff'),
    ('external_web', 'Customer Web Form'),
    ('manual', 'Manual'),
]


class ResPartner(models.Model):
    _inherit = 'res.partner'

    is_web_customer = fields.Boolean(
        string='Web Customer',
        default=False,
        index=True,
        help='Created via mintdeals.com / shop.letsgomint.us signup. '
             'Record rules restrict visibility to privileged users only.',
    )

    # ── Communication consent (mirrors the SMS opt-in shape in mint_sms_telnyx) ──
    # SMS consent (sms_opt_in / sms_opt_out) lives in mint_sms_telnyx. Email and
    # Call consent live here so the Astro account-preferences UI (/api/customer/
    # preferences) has one place to read/write them. Consent-first policy: web
    # customers are opted OUT by default; only TRANSACTIONAL/security email is
    # exempt — marketing/discretionary sends must check _marketing_email_allowed().
    email_opt_in = fields.Boolean(
        string='Email Opt-In', default=False, index=True,
        help='Customer consented to MARKETING/promotional email. Transactional '
             'email (password reset, order, verification) is exempt and always sends.',
    )
    email_opt_in_date = fields.Datetime(string='Email Opt-In Date', readonly=True)
    email_opt_in_source = fields.Selection(OPT_IN_SOURCES, string='Email Opt-In Source')

    call_opt_in = fields.Boolean(
        string='Call Opt-In', default=False, index=True,
        help='Customer consented to receive phone calls. (No programmatic call '
             'channel exists today — recorded consent only.)',
    )
    call_opt_in_date = fields.Datetime(string='Call Opt-In Date', readonly=True)
    call_opt_in_source = fields.Selection(OPT_IN_SOURCES, string='Call Opt-In Source')

    def set_email_opt_in(self, source='manual'):
        """Record marketing-email consent + when/where it came from."""
        self.write({
            'email_opt_in': True,
            'email_opt_in_date': fields.Datetime.now(),
            'email_opt_in_source': source,
        })
        return True

    def set_email_opt_out(self):
        self.write({'email_opt_in': False})
        return True

    def set_call_opt_in(self, source='manual'):
        self.write({
            'call_opt_in': True,
            'call_opt_in_date': fields.Datetime.now(),
            'call_opt_in_source': source,
        })
        return True

    def set_call_opt_out(self):
        self.write({'call_opt_in': False})
        return True

    def _marketing_email_allowed(self):
        """Consent gate for DISCRETIONARY / marketing email (welcome blast,
        mass_mailing, promos). Non-web-customer partners (staff, vendors) are
        unaffected; a web customer must have opted in. Transactional/security
        email must NOT call this — it always sends. Wire this into the welcome
        template send + mailing recipient filter (see MR#680)."""
        self.ensure_one()
        return bool(self.email_opt_in or not self.is_web_customer)
