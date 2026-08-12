# -*- coding: utf-8 -*-
from odoo import models, fields


class ResPartner(models.Model):
    _inherit = 'res.partner'

    is_web_customer = fields.Boolean(
        string='Web Customer',
        default=False,
        index=True,
        help='Created via mintdeals.com / shop.letsgomint.us signup. '
             'Record rules restrict visibility to privileged users only.',
    )

    phone_bonus_granted = fields.Boolean(
        string='Phone-Add Bonus Granted',
        default=False,
        help='Set once the one-time loyalty bonus for adding a phone number '
             'has been issued; prevents re-grants if the phone is removed '
             'and re-added.',
    )

    # --- Age-verification ledger -------------------------------------------
    # Odoo is the authoritative record of the web-side age check. The legally
    # binding verification still happens at the POS when a budtender scans the
    # physical ID; this captures what the storefront collected so it is
    # provable and can be linked/pushed to the Dutchie customer record
    # (POST /customer/customer dateOfBirth, linked via externalId).
    web_date_of_birth = fields.Date(
        string='Date of Birth (web)',
        help='DOB collected during web signup. Source of the 21+ check.',
    )
    age_verified = fields.Boolean(
        string='Age Verified (21+)',
        default=False,
        index=True,
        help='True once the web signup passed the 21+ check. Note: this is '
             'the web-side pre-screen, not the in-store POS ID scan.',
    )
    age_verified_at = fields.Datetime(
        string='Age Verified At',
        help='Timestamp the web 21+ check passed (audit).',
    )
    age_verification_method = fields.Selection(
        [
            ('self_attested', 'Self-attested DOB'),
            ('id_scanned', 'ID document scan (web)'),
            ('pos_scanned', 'In-store POS ID scan'),
        ],
        string='Age Verification Method',
        help='How the age was verified. self_attested = typed DOB; '
             'id_scanned = web IdScanner; pos_scanned = Dutchie POS ID scan.',
    )
    age_verification_source = fields.Char(
        string='Age Verification Source',
        help='Provenance of the verification event, e.g. web_register, '
             'web_google, pos_import.',
    )
