# -*- coding: utf-8 -*-
"""
Extend res.partner with Dutchie customer fields for daily sync.

Sensitive PII (date of birth, driver's license, MJ state ID) is encrypted at
rest with Fernet: the ciphertext lives in ``*_enc`` columns and the plaintext is
exposed only through non-stored compute/inverse fields, both gated behind the
``group_dutchie_pii`` security group. See ``pii_crypto.py`` for key handling.
"""
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .pii_crypto import decrypt_value, encrypt_value, get_cipher

_logger = logging.getLogger(__name__)

PII_GROUP = 'mint_dutchie_sync.group_dutchie_pii'


class ResPartner(models.Model):
    _inherit = 'res.partner'

    # Dutchie customer linkage
    x_dutchie_customer_id = fields.Char(
        string='Dutchie Customer ID',
        index=True,
        copy=False,
        help='Unique customer ID from Dutchie Backoffice (maintenance/get-customers).',
    )
    x_dutchie_customer_code = fields.Char(
        string='Dutchie Customer Code',
        index=True,
        copy=False,
        help='Customer code from Dutchie (e.g., state ID or patient number).',
    )
    x_dutchie_identity_key = fields.Char(
        string='Dutchie Identity Key',
        index=True,
        copy=False,
        help='Normalized cross-store identity (DL > MJ state ID > Name+DOB > '
             'phone) used to dedup the SAME person across stores. This is the '
             'roster-sync match key — NOT the per-location customer id. No DB '
             'UNIQUE constraint: the sync searches then upserts so a collision '
             'never aborts a chunk.',
    )

    # Home store assignment
    x_home_store_id = fields.Many2one(
        'res.company',
        string='Home Store',
        help='Primary dispensary location for this customer.',
    )

    # Sync metadata
    x_dutchie_last_sync = fields.Datetime(
        string='Last Dutchie Sync',
        copy=False,
        help='When this customer was last synced from Dutchie.',
    )
    x_dutchie_first_visit = fields.Date(
        string='First Visit',
        copy=False,
    )
    x_dutchie_total_spend = fields.Float(
        string='Total Spend (Dutchie)',
        digits=(12, 2),
        copy=False,
        help='Running total of net spend imported from Dutchie.',
    )
    x_dutchie_visit_count = fields.Integer(
        string='Visit Count',
        copy=False,
        help='Total number of transactions imported from Dutchie.',
    )

    # Purchase log (one2many)
    x_dutchie_purchase_ids = fields.One2many(
        'mint.dutchie.purchase',
        'partner_id',
        string='Dutchie Purchases',
    )

    # ------------------------------------------------------------------
    # Encrypted PII — ciphertext columns (stored) + plaintext compute/inverse.
    # All gated behind group_dutchie_pii so non-members never read or write them.
    # ------------------------------------------------------------------
    x_dutchie_dob_enc = fields.Char(
        string='DOB (encrypted)', copy=False, groups=PII_GROUP,
        help='Fernet ciphertext of the Dutchie patient date of birth.',
    )
    x_dutchie_dl_enc = fields.Char(
        string="Driver's License (encrypted)", copy=False, groups=PII_GROUP,
        help='Fernet ciphertext of the Dutchie driver license number.',
    )
    x_dutchie_mj_state_id_enc = fields.Char(
        string='MJ State ID (encrypted)', copy=False, groups=PII_GROUP,
        help='Fernet ciphertext of the medical-marijuana state ID number.',
    )

    x_dutchie_dob = fields.Char(
        string='Date of Birth',
        compute='_compute_dutchie_pii', inverse='_inverse_dutchie_dob',
        store=False, groups=PII_GROUP,
        help='Dutchie patient DOB. Encrypted at rest; restricted to the '
             'Dutchie Customer PII group.',
    )
    x_dutchie_dl = fields.Char(
        string="Driver's License",
        compute='_compute_dutchie_pii', inverse='_inverse_dutchie_dl',
        store=False, groups=PII_GROUP,
        help='Encrypted at rest; restricted to the Dutchie Customer PII group.',
    )
    x_dutchie_mj_state_id = fields.Char(
        string='MJ State ID',
        compute='_compute_dutchie_pii', inverse='_inverse_dutchie_mj_state_id',
        store=False, groups=PII_GROUP,
        help='Encrypted at rest; restricted to the Dutchie Customer PII group.',
    )

    # ------------------------------------------------------------------
    # Non-sensitive Dutchie demographics
    # ------------------------------------------------------------------
    x_dutchie_mj_id_expiration = fields.Date(
        string='MMJ ID Expiration', copy=False,
    )
    # patient_type / gender stored as raw Dutchie strings (passthrough), not a
    # Selection: the exact enum values are not yet verified against a live
    # get-customers payload, so a fixed Selection would risk a key mismatch when
    # the step-2 sync mapper lands. Tighten to a Selection once values are pinned.
    x_dutchie_patient_type = fields.Char(
        string='Patient Type', copy=False,
        help='Raw Dutchie patient type (e.g. medical/recreational); passthrough '
             'pending verification of live values.',
    )
    x_dutchie_patient_status = fields.Char(
        string='Patient Status', copy=False,
    )
    x_dutchie_gender = fields.Char(
        string='Gender', copy=False,
        help='Raw Dutchie gender value; passthrough pending verification.',
    )
    x_dutchie_member_since = fields.Date(
        string='Member Since', copy=False,
    )

    # ------------------------------------------------------------------
    # Loyalty
    # ------------------------------------------------------------------
    # NOTE: x_dutchie_loyalty_id is owned by mint_pos_bridge (pos_partner.py),
    # which depends on this module — do NOT redeclare it here (duplicate index
    # on res.partner). Only the balance is new.
    x_dutchie_loyalty_balance = fields.Float(
        string='Loyalty Points Balance', digits=(12, 2), copy=False,
    )

    # --- Onboarding: 21+ verification (Odoo #101243) ---
    # Persisted at web signup from the ID scan. We store only the age-verified
    # flag + DOB — NOT the license number/image (PII-minimization; ticket
    # decision #3). The driver's-license fields from the earlier signup-v2
    # attempt (0bd7454) were removed as Odoo-19 schema casualties and are
    # intentionally not reintroduced.
    x_age_verified = fields.Boolean(
        string='Age Verified (21+)',
        help='Set when a scanned government ID confirmed the customer is 21 or older.',
    )
    x_date_of_birth = fields.Date(
        string='Date of Birth',
        help='Customer DOB captured at web signup (ID scan), used for 21+ verification.',
    )

    _dutchie_customer_id_unique = models.Constraint(
        'UNIQUE(x_dutchie_customer_id)',
        'Dutchie Customer ID must be unique.',
    )

    @api.depends('x_dutchie_dob_enc', 'x_dutchie_dl_enc', 'x_dutchie_mj_state_id_enc')
    def _compute_dutchie_pii(self):
        # Resolve the cipher ONCE per recordset, not per record/field.
        cipher = get_cipher(self.env)
        for rec in self:
            rec.x_dutchie_dob = decrypt_value(self.env, rec.x_dutchie_dob_enc, cipher=cipher)
            rec.x_dutchie_dl = decrypt_value(self.env, rec.x_dutchie_dl_enc, cipher=cipher)
            rec.x_dutchie_mj_state_id = decrypt_value(
                self.env, rec.x_dutchie_mj_state_id_enc, cipher=cipher)

    def _store_encrypted_pii(self, plain_field, enc_field):
        """Encrypt one plaintext PII field into its ``_enc`` column.

        Skips records whose plaintext is unchanged vs. the current ciphertext —
        this avoids (a) churning the column with a fresh non-deterministic token
        on every save and (b) the data-loss path where a wrong/rotated key makes
        decrypt return blank and a re-save would otherwise overwrite good
        ciphertext with an encryption of the blank value. Raises a clean
        UserError (not a raw 500) when a real value must be stored but no key is
        configured.
        """
        cipher = get_cipher(self.env)
        for rec in self:
            new_plain = rec[plain_field] or False
            current_plain = decrypt_value(self.env, rec[enc_field], cipher=cipher) or False
            if new_plain == current_plain:
                continue  # unchanged (or both blank) → never clobber ciphertext
            if new_plain and cipher is None:
                raise UserError(_(
                    "Dutchie PII encryption key is not configured; cannot store "
                    "%s. Set the DUTCHIE_PII_FERNET_KEY environment variable."
                ) % plain_field)
            rec[enc_field] = encrypt_value(self.env, new_plain, cipher=cipher) if new_plain else False

    def _inverse_dutchie_dob(self):
        self._store_encrypted_pii('x_dutchie_dob', 'x_dutchie_dob_enc')

    def _inverse_dutchie_dl(self):
        self._store_encrypted_pii('x_dutchie_dl', 'x_dutchie_dl_enc')

    def _inverse_dutchie_mj_state_id(self):
        self._store_encrypted_pii('x_dutchie_mj_state_id', 'x_dutchie_mj_state_id_enc')
