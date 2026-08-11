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

from .pii_crypto import blind_index, decrypt_value, encrypt_value, get_cipher

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

    # ------------------------------------------------------------------
    # Blind index — the joinable form of the licence number.
    #
    # x_dutchie_identity_key holds 1,661,769 licence numbers IN THE CLEAR
    # because the welcome signup path must match on them, and Fernet — being
    # non-deterministic — offers nothing to match against. This index closes
    # that gap: same join, no plaintext.
    # ------------------------------------------------------------------
    x_dl_index = fields.Char(
        string='DL Blind Index',
        index=True,
        copy=False,
        groups=PII_GROUP,
        help='HMAC-SHA256 of the normalised licence number, keyed by a pepper '
             'derived from the Fernet key. Deterministic so it can be joined, '
             'one-way so a database dump yields no licence numbers. Normalised '
             'more aggressively than x_dutchie_identity_key (punctuation '
             'stripped), which recovers ~93,707 keys exact-match misses today.',
    )
    x_dl_ambiguous = fields.Boolean(
        string='DL Index Ambiguous',
        default=False,
        copy=False,
        index=True,
        help='Set when this partner shares a blind index with another — two '
             'licence numbers differing only by punctuation. Measured at 891 '
             'pairs across 1.66M partners. These MUST NOT be auto-matched: a '
             'wrong link hands one customer another customer history. Route '
             'them through the existing x_merge_needs_review confirmation.',
    )
    x_dutchie_lsp_id = fields.Char(
        string='Dutchie LSP',
        index=True,
        copy=False,
        help='Which Dutchie tenant (LspId) this customer belongs to — AZ 575, '
             'MI 576, MO 723, IL 805, NV 820, FL 821. Customer ids are scoped '
             'per LSP and the loyalty ledger is read from an LSP-scoped '
             'session, so an id without its LSP is ambiguous and will quietly '
             'return an empty balance against the wrong tenant.',
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

    # ------------------------------------------------------------------
    # Blind-index backfill
    # ------------------------------------------------------------------
    @api.model
    def _backfill_dl_index(self, batch=5000, limit=None):
        """Populate x_dl_index from the plaintext in x_dutchie_identity_key.

        Idempotent and resumable: only touches rows whose index is unset, so
        a crashed run is restarted by calling it again. Does NOT clear the
        plaintext — cutting the signup path over to the index is a separate,
        reversible step, and deleting the source before the join is proven
        would be unrecoverable.

        Returns {'indexed': n, 'skipped': n}.
        """
        if not get_cipher(self.env):
            raise UserError(_(
                'Dutchie PII encryption key is not configured; refusing to '
                'build a blind index without a pepper.'
            ))
        domain = [('x_dutchie_identity_key', 'like', 'dl:%'), ('x_dl_index', '=', False)]
        done = skipped = 0
        while True:
            batch_ids = self.search(domain, limit=batch)
            if not batch_ids:
                break
            for rec in batch_ids:
                raw = (rec.x_dutchie_identity_key or '')[3:]
                idx = blind_index(self.env, raw)
                if not idx:
                    # Unusable source (measured: 3,393 barcode fragments and
                    # over/under-length keys). Leave the index unset — it would
                    # never have matched anything anyway.
                    skipped += 1
                    continue
                rec.with_context(tracking_disable=True, mail_notrack=True).x_dl_index = idx
                done += 1
            self.env.cr.commit()  # checkpoint: 1.66M rows is not one transaction
            _logger.info('DL blind index: %d indexed, %d skipped', done, skipped)
            if limit and done >= limit:
                break
        return {'indexed': done, 'skipped': skipped}

    @api.model
    def _flag_ambiguous_dl_index(self):
        """Flag partners whose blind index is shared with another partner.

        THE GUARD. Normalising away punctuation recovers ~93,707 otherwise
        unmatchable keys, but it also merges 891 pairs that were distinct
        while the punctuation survived. Auto-matching those would hand one
        customer another customer's purchase history — a privacy incident,
        not a data-quality one — so they are marked and excluded from
        automatic linking, then confirmed by staff at the register through
        the existing x_merge_needs_review flow.

        Returns the number of partners flagged.
        """
        groups = self.read_group(
            [('x_dl_index', '!=', False)], ['id'], ['x_dl_index'], lazy=False,
        )
        dupes = [g['x_dl_index'] for g in groups if g['__count'] > 1]
        if not dupes:
            return 0
        flagged = self.search([('x_dl_index', 'in', dupes)])
        flagged.with_context(tracking_disable=True, mail_notrack=True).write(
            {'x_dl_ambiguous': True})
        _logger.warning(
            'DL blind index: %d partners across %d colliding indexes flagged '
            'ambiguous — excluded from auto-match', len(flagged), len(dupes),
        )
        return len(flagged)
