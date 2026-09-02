# -*- coding: utf-8 -*-
"""
Extend res.partner with Dutchie customer fields for daily sync.

Sensitive PII (date of birth, driver's license, MJ state ID) is encrypted at
rest with Fernet: the ciphertext lives in ``*_enc`` columns and the plaintext is
exposed only through non-stored compute/inverse fields, both gated behind the
``group_dutchie_pii`` security group. See ``pii_crypto.py`` for key handling.
"""
import logging

from psycopg2.extras import execute_values

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
        # Paginate by ascending id, NOT by "x_dl_index is unset". The 3,393
        # unusable keys can never be indexed, so a domain-based loop would
        # re-select them on every pass and never terminate. An id cursor
        # advances regardless of whether the row produced an index.
        done = skipped = 0
        last_id = 0
        while True:
            rows = self.search_read(
                [('x_dutchie_identity_key', 'like', 'dl:%'),
                 ('x_dl_index', '=', False),
                 ('id', '>', last_id)],
                ['id', 'x_dutchie_identity_key'],
                limit=batch, order='id asc',
            )
            if not rows:
                break
            last_id = rows[-1]['id']
            pairs = []
            for r in rows:
                idx = blind_index(self.env, (r['x_dutchie_identity_key'] or '')[3:])
                if idx:
                    pairs.append((r['id'], idx))
                else:
                    # Unusable source (measured: 3,393 barcode fragments and
                    # over/under-length keys). Left unset deliberately — it
                    # would never have matched anything anyway.
                    skipped += 1
            if pairs:
                # One statement per batch rather than 5,000 ORM writes: at
                # 1.66M rows the per-record path is the difference between
                # minutes and hours, and every ORM write would also fire
                # compute/tracking machinery this column does not need.
                execute_values(
                    self.env.cr,
                    'UPDATE res_partner p SET x_dl_index = v.idx '
                    'FROM (VALUES %s) AS v(id, idx) WHERE p.id = v.id::int',
                    pairs,
                )
                done += len(pairs)
            self.env.cr.commit()  # checkpoint: 1.66M rows is not one transaction
            _logger.info('DL blind index: %d indexed, %d skipped (through id %d)',
                         done, skipped, last_id)
            if limit and done >= limit:
                break
        self.env['res.partner'].invalidate_model(['x_dl_index'])
        return {'indexed': done, 'skipped': skipped}

    # SQL, not read_group. An ORM read_group over this column asks Postgres to
    # build ~1.66M groups and then ships every one of them back through
    # XML-RPC; measured against the same-cardinality x_dutchie_identity_key it
    # had not returned after nine minutes. That matters more than it looks:
    # if the guard times out, it does not fail loudly — it simply never runs,
    # and the colliding partners stay unflagged and therefore auto-matchable.
    # A guard that fails OPEN is worse than no guard, because it is trusted.
    # These statements resolve entirely server-side and return a row count.
    _DUPE_CTE = """
        WITH dupes AS (
            SELECT x_dl_index
              FROM res_partner
             WHERE x_dl_index IS NOT NULL AND x_dl_index != ''
             GROUP BY x_dl_index
            HAVING count(*) > 1
        )
    """

    @api.model
    def _dl_index_collision_count(self):
        """Read-only: how many partners WOULD be flagged. Changes nothing.

        Exists so the collision count can be inspected on production before
        anything is written — a dry run for the guard.
        """
        self.env.cr.execute(self._DUPE_CTE + """
            SELECT count(*) AS partners, count(DISTINCT p.x_dl_index) AS indexes
              FROM res_partner p JOIN dupes d ON p.x_dl_index = d.x_dl_index
        """)
        row = self.env.cr.dictfetchone() or {}
        return {'partners': row.get('partners', 0), 'indexes': row.get('indexes', 0)}

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

        Idempotent in BOTH directions: re-running clears flags that are no
        longer ambiguous, so a collision resolved by correcting one record
        does not leave the other permanently un-matchable.

        Returns {'flagged': n, 'cleared': n}.
        """
        # Set first, then clear, both scoped by the same CTE.
        self.env.cr.execute(self._DUPE_CTE + """
            UPDATE res_partner p
               SET x_dl_ambiguous = true
              FROM dupes d
             WHERE p.x_dl_index = d.x_dl_index
               AND p.x_dl_ambiguous IS DISTINCT FROM true
        """)
        flagged = self.env.cr.rowcount
        self.env.cr.execute(self._DUPE_CTE + """
            UPDATE res_partner p
               SET x_dl_ambiguous = false
             WHERE p.x_dl_ambiguous = true
               AND NOT EXISTS (
                   SELECT 1 FROM dupes d WHERE d.x_dl_index = p.x_dl_index
               )
        """)
        cleared = self.env.cr.rowcount
        # Raw SQL bypasses the ORM cache; without this, records already loaded
        # in this transaction keep reporting the pre-update value.
        self.env['res.partner'].invalidate_model(['x_dl_ambiguous'])
        if flagged or cleared:
            _logger.warning(
                'DL blind index guard: %d partner(s) flagged ambiguous, %d '
                'cleared — flagged rows are excluded from auto-match',
                flagged, cleared,
            )
        return {'flagged': flagged, 'cleared': cleared}

    # ── Query-time identity resolution ───────────────────────────────────
    #
    # THE one definition of "which partner rows are this same human, for the
    # purpose of a read". It lives here because this module owns
    # x_dutchie_identity_key, and it is a public model method precisely so
    # every consumer can share it instead of re-deriving identity:
    #
    #   * mint_pos_bridge  — /api/v1/pos/orders (the customer's own view)
    #   * mint_customer_api — loyalty balance behind /api/v1/customer/loyalty,
    #                         which is also what decides whether the Apple
    #                         Wallet button offers a card at all
    #   * mintinvsvc       — the Wallet pass auto-update, over execute_kw
    #
    # A fourth implementation of "who is this person?" is exactly the failure
    # this workstream exists to end, so resist adding one.

    # Only these tiers are ever unioned. 'nd:' (name+DOB) and 'ph:' (phone) are
    # excluded by construction: 'ph:' keys are built from the raw roster string
    # and junk numbers like 0000000000 are real rows, while stored 'nd:' keys
    # include entries such as "nd:* *|08/28/1986".
    IDENTITY_STRONG_PREFIXES = ('dl:', 'mj:')

    # A key that is only its prefix is a sentinel, not an identity.
    IDENTITY_SENTINEL_KEYS = frozenset(['', 'dl:', 'mj:', 'nd:', 'ph:', 'nd:|'])

    # Legacy key name: this flag shipped with the /orders change before the
    # resolver was shared. Renaming a switch that is already set in production
    # buys nothing and risks a window where neither name is live, so it keeps
    # its original name and now gates identity resolution everywhere.
    IDENTITY_UNION_PARAM = 'mint_pos_bridge.orders_identity_union'
    IDENTITY_UNION_MAX_PARAM = 'mint_pos_bridge.orders_identity_union_max'
    IDENTITY_UNION_MAX_DEFAULT = 10

    # Failure bookkeeping for _log_identity_failure. Deliberately plain class
    # state: it is per-worker under prefork, which is fine — each worker
    # reports its own rate, and losing it on restart costs one extra traceback,
    # not correctness. Nothing here may be relied on for behaviour.
    _identity_failure_state = {'count': 0, 'last_logged': None}


    @staticmethod
    def _identity_base_email(email):
        """Normalise an address to the mailbox that actually receives it.

        'jpalomino+123@brightroot.com' and 'jpalomino@brightroot.com' are one
        mailbox, so they are one person for the claimed-account check below.
        A different local part or domain is a different person.
        """
        email = (email or '').strip().lower()
        if '@' not in email:
            return ''
        local, _sep, domain = email.partition('@')
        return '%s@%s' % (local.split('+', 1)[0], domain)

    def identity_union_ids(self):
        """Partner ids this record's own view may resolve to, self included.

        Public so it can be called over execute_kw by mintinvsvc.

        Returns [self.id] unless all of these hold: the flag is on, this
        partner carries a STRONG identity key that is not a sentinel, the key
        resolves to a small group, and each sibling is either unclaimed or
        claimed by a login delivering to the same mailbox.

        Never raises. Widening a read must not be able to take down the caller,
        so anything unexpected degrades to the single id — which is the
        behaviour every consumer had before this existed.
        """
        self.ensure_one()
        try:
            ICP = self.env['ir.config_parameter'].sudo()
            flag = str(ICP.get_param(self.IDENTITY_UNION_PARAM, '0')).strip().lower()
            if flag not in ('1', 'true', 'yes'):
                return [self.id]

            Partner = self.env['res.partner'].sudo()
            me = self.sudo()
            key = (me.x_dutchie_identity_key or '').strip()
            if (not key
                    or key in self.IDENTITY_SENTINEL_KEYS
                    or not key.startswith(self.IDENTITY_STRONG_PREFIXES)):
                return [self.id]

            try:
                max_group = int(ICP.get_param(
                    self.IDENTITY_UNION_MAX_PARAM, self.IDENTITY_UNION_MAX_DEFAULT))
            except (TypeError, ValueError):
                max_group = self.IDENTITY_UNION_MAX_DEFAULT

            # limit+1 so an oversized group is detectable, not silently trimmed.
            siblings = Partner.search(
                [('x_dutchie_identity_key', '=', key), ('id', '!=', me.id)],
                limit=max_group + 1,
            )
            if len(siblings) > max_group:
                _logger.warning(
                    'identity-union: key held by partner %s resolves to more '
                    'than %s partners — refusing to union', me.id, max_group)
                return [self.id]

            my_mailbox = self._identity_base_email(me.email)
            allowed = []
            for sib in siblings:
                if sib.employee or any(not u.share for u in sib.user_ids):
                    # Staff record — never fold into a customer's view.
                    continue
                if sib.user_ids:
                    # A sibling someone can log into is a separate claimed
                    # account UNLESS it delivers to the caller's mailbox (a
                    # '+alias' signup by the same human). No mailbox to
                    # compare means no proof, so exclude.
                    if (not my_mailbox
                            or self._identity_base_email(sib.email) != my_mailbox):
                        _logger.warning(
                            'identity-union: sibling %s of partner %s is a '
                            'separately claimed account — excluded',
                            sib.id, me.id)
                        continue
                allowed.append(sib.id)

            if allowed:
                _logger.info(
                    'identity-union: partner %s resolved to %s', me.id, allowed)
            return [self.id] + allowed
        except Exception as exc:  # noqa: BLE001 — a read widening must never break a caller
            self._log_identity_failure(exc)
            return [self.id]

    @api.model
    def _log_identity_failure(self, exc):
        """Report a resolution failure loudly once, then quietly with a count.

        Two things make the naive `_logger.exception(...)` here a bad fit.

        It is a HOT path — every authenticated /orders call and every loyalty
        read goes through it — so a systemic failure (a renamed field, a half
        applied upgrade) writes one full traceback per request into a log that
        has no drain. The evidence of the problem buries the evidence of
        everything else.

        And the fallback is SILENT BY DESIGN: returning [self.id] is exactly
        what a customer with no siblings looks like. So a total outage renders
        as "nobody has siblings today" on both /orders and the Wallet balance —
        correct-looking, quietly wrong, and indistinguishable from healthy.
        The count below is what tells those two apart.

        First failure gets the traceback. After that, one WARNING a minute
        carrying how many were swallowed in between, so the signal survives at
        any failure rate.
        """
        state = ResPartner._identity_failure_state
        now = fields.Datetime.now()
        state['count'] += 1
        first = state['last_logged'] is None
        elapsed = None if first else (now - state['last_logged']).total_seconds()

        if first:
            state['last_logged'] = now
            state['count'] = 0
            _logger.exception(
                'identity-union: resolution FAILED — falling back to strict '
                'single-partner. This degrades silently: a caller cannot tell '
                'it from a customer who genuinely has no siblings.')
        elif elapsed is not None and elapsed >= 60:
            swallowed = state['count']
            state['last_logged'] = now
            state['count'] = 0
            _logger.warning(
                'identity-union: %d further resolution failure(s) in the last '
                '%ds, all falling back to strict. Latest: %s: %s',
                swallowed, int(elapsed), type(exc).__name__, exc)
