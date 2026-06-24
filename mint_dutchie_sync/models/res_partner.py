# -*- coding: utf-8 -*-
"""
Extend res.partner with Dutchie customer fields for daily sync.

Sensitive PII (date of birth, driver's license, MJ state ID) is encrypted at
rest with Fernet: the ciphertext lives in ``*_enc`` columns and the plaintext is
exposed only through non-stored compute/inverse fields, both gated behind the
``group_dutchie_pii`` security group. See ``pii_crypto.py`` for key handling.
"""
import logging

from odoo import api, fields, models

from .pii_crypto import decrypt_value, encrypt_value

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
    x_dutchie_patient_type = fields.Selection(
        [('medical', 'Medical'), ('recreational', 'Recreational')],
        string='Patient Type', copy=False,
    )
    x_dutchie_patient_status = fields.Char(
        string='Patient Status', copy=False,
    )
    x_dutchie_gender = fields.Selection(
        [('male', 'Male'), ('female', 'Female'),
         ('other', 'Other'), ('unknown', 'Unknown')],
        string='Gender', copy=False,
    )
    x_dutchie_member_since = fields.Date(
        string='Member Since', copy=False,
    )

    # ------------------------------------------------------------------
    # Loyalty
    # ------------------------------------------------------------------
    x_dutchie_loyalty_id = fields.Char(
        string='Dutchie Loyalty ID', index=True, copy=False,
    )
    x_dutchie_loyalty_balance = fields.Float(
        string='Loyalty Points Balance', digits=(12, 2), copy=False,
    )

    _dutchie_customer_id_unique = models.Constraint(
        'UNIQUE(x_dutchie_customer_id)',
        'Dutchie Customer ID must be unique.',
    )

    @api.depends('x_dutchie_dob_enc', 'x_dutchie_dl_enc', 'x_dutchie_mj_state_id_enc')
    def _compute_dutchie_pii(self):
        for rec in self:
            rec.x_dutchie_dob = decrypt_value(self.env, rec.x_dutchie_dob_enc)
            rec.x_dutchie_dl = decrypt_value(self.env, rec.x_dutchie_dl_enc)
            rec.x_dutchie_mj_state_id = decrypt_value(
                self.env, rec.x_dutchie_mj_state_id_enc)

    def _inverse_dutchie_dob(self):
        for rec in self:
            rec.x_dutchie_dob_enc = encrypt_value(self.env, rec.x_dutchie_dob)

    def _inverse_dutchie_dl(self):
        for rec in self:
            rec.x_dutchie_dl_enc = encrypt_value(self.env, rec.x_dutchie_dl)

    def _inverse_dutchie_mj_state_id(self):
        for rec in self:
            rec.x_dutchie_mj_state_id_enc = encrypt_value(
                self.env, rec.x_dutchie_mj_state_id)
