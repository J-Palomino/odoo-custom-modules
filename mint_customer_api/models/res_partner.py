# -*- coding: utf-8 -*-
from odoo import api, models, fields


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

    # ------------------------------------------------------------------
    # Canonical "is this partner a customer?" predicate
    # ------------------------------------------------------------------
    # One definition, shared by every customer-isolation guard. It mirrors the
    # domains already used by the staff-flag guard (mint_pos_bridge) and the
    # Dutchie roster backfill (mint_dutchie_sync) so all three agree on who
    # counts as a customer:
    #
    #   customer = carries a customer marker AND is not staff
    #   marker   = web signup, Dutchie origin, or a Dutchie customer id
    #   staff    = an employee work-contact, or a partner backing a
    #              non-share (internal) user
    #
    # The staff carve-out matters: employees shop here too, so one partner can
    # carry both. Staff always wins — an employee who bought something is
    # still an employee. (The hourly staff-flag guard in mint_pos_bridge
    # actively strips customer markers off staff partners, which keeps the two
    # sets disjoint in practice; this carve-out is what keeps the guards
    # correct in the window before that cron runs.)

    CUSTOMER_MARKER_ORIGINS = ['dutchie_walkin', 'dutchie_weborder', 'web_checkout']

    def _mint_is_staff(self, ignore_users=None):
        """True when this partner is staff (in-memory, no search).

        ``ignore_users`` excludes specific user records from the internal-user
        test. The guard that stops customers from *becoming* internal users
        needs it: by the time it runs the account already exists, so counting
        it would make every partner look like staff and the check would never
        fire.
        """
        self.ensure_one()
        if self.employee:
            return True
        users = self.user_ids - (ignore_users or self.env['res.users'])
        return any(not u.share for u in users)

    def _mint_is_customer(self, ignore_users=None):
        """True when this partner is a genuine customer (in-memory, no search)."""
        self.ensure_one()
        if self._mint_is_staff(ignore_users=ignore_users):
            return False
        return bool(
            self.is_web_customer
            or self.x_partner_origin in self.CUSTOMER_MARKER_ORIGINS
            or self.x_dutchie_customer_id
        )

    def _mint_filter_customers(self):
        """Return the subset of this recordset that are genuine customers."""
        return self.filtered(lambda p: p._mint_is_customer())

    # Stored and indexed, not computed on the fly, purely for query speed.
    # This is the column the follower picker filters on, and that search runs
    # `name ilike` against ~2.7M partners. Measured on production: the same
    # picker query costs ~0.2s unfiltered, ~0.6s filtered on an *indexed*
    # column, and 4-9s filtered on anything unindexed (or on a large literal
    # id list). Selectivity is not what matters here — the index is — so the
    # flag has to be a real indexed column to keep the dialog usable.
    is_customer_contact = fields.Boolean(
        string='Is Customer Contact',
        compute='_compute_is_customer_contact',
        store=True,
        index=True,
        help='Carries a customer marker and is not staff. Used to keep '
             'customers out of internal-only pickers such as ticket followers.',
    )

    @api.depends('is_web_customer', 'x_partner_origin', 'x_dutchie_customer_id',
                 'employee', 'user_ids.share')
    def _compute_is_customer_contact(self):
        for partner in self:
            partner.is_customer_contact = partner._mint_is_customer()
