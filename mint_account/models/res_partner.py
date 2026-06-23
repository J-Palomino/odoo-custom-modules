# -*- coding: utf-8 -*-
from odoo import models, fields


# Mirror of the SMS opt-in source list (mint_sms_telnyx), minus the SMS-only
# 'sms_keyword'. external_web = set by the customer via the Astro account profile.
OPT_IN_SOURCES = [
    ('internal_staff', 'Internal Staff'),
    ('external_web', 'Customer Web Form'),
    ('manual', 'Manual'),
]


class ResPartner(models.Model):
    _inherit = 'res.partner'

    # Communication consent for the non-SMS channels (SMS opt-in/out live in
    # mint_sms_telnyx). Lives here in mint_account — which IS in the persistent
    # ODOO_UPDATE_MODULES list — so the routine entrypoint -u creates the
    # columns reliably (mint_customer_api's -u does not; it broke prod once).
    #
    # NO explicit default: an unset Boolean reads as False (= opted out), and
    # omitting the default keeps these out of the res.partner INSERT, so a plain
    # restart that loads the field before -u creates the column does NOT break
    # res.partner.create. Consent-first: web customers are opted out until they
    # opt in; only MARKETING/discretionary sends check _marketing_email_allowed().
    email_opt_in = fields.Boolean(string='Email Opt-In', index=True)
    email_opt_in_date = fields.Datetime(string='Email Opt-In Date', readonly=True)
    email_opt_in_source = fields.Selection(OPT_IN_SOURCES, string='Email Opt-In Source')

    call_opt_in = fields.Boolean(string='Call Opt-In', index=True)
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
        mass_mailing, promos). Staff/vendors pass; a web customer must have
        opted in. Transactional/security email must NOT call this."""
        self.ensure_one()
        return bool(self.email_opt_in or not self.is_web_customer)
