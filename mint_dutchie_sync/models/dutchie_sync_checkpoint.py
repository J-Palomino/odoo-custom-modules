# -*- coding: utf-8 -*-
"""
Resumable per-store checkpoint + cron for backfilling Dutchie customer rosters
into res.partner.

Each Dutchie store (one LocId) gets one ``mint.dutchie.sync.checkpoint`` row
tracking progress. The cron processes ONE bounded chunk per fire (one page of
one store), advances the checkpoint, and stops — so the multi-million-row
roster imports persistently across many short, retry-safe ticks rather than one
long call that would hit Dutchie's export 502/timeout limits.

Layering:
  * ``_fetch_roster_page`` is an integration SEAM (step 2b) — it must return one
    page of Patient Contact Report (ReportId 125) rows for a LocId. Re-implement
    it against the proven Dutchie Backoffice client; everything below it is
    deterministic and verified.
  * ``_identity_key`` / ``_upsert_partner`` are the dedup + encrypted-upsert
    logic proven in the V1 verification run.

The report-125 column names below were confirmed present on a live run-report
response (per CLAUDE.md: external field names used verbatim from a real call).
"""
import logging

from odoo import _, api, fields, models

from .pii_crypto import get_cipher

_logger = logging.getLogger(__name__)

# Verified Patient Contact Report (ReportId 125) column -> res.partner mapping.
# Plaintext demographics + identifiers; PII (DOB/DL/MJ) is routed through the
# encrypted compute/inverse fields, not written here as plaintext columns.
ROSTER_COLS = {
    'id': 'Id',                      # -> x_dutchie_customer_id (per-location row id)
    'name': 'Accts_Name',
    'dob': 'PatientDOB',             # -> x_dutchie_dob (encrypted)
    'dl': 'DriversLicense',          # -> x_dutchie_dl (encrypted)
    'mj_state_id': 'MJStateIDNo',    # -> x_dutchie_mj_state_id (encrypted)
    'mj_expiration': 'MMJExpiration',
    'patient_type': 'patientType',
    'patient_status': 'PatientStatus',
    'gender': 'Gender',
    'member_since': 'MemberSince',
    'street': 'Accts_Addr1',
    'city': 'City',
    'zip': 'PostalCode',
    'phone': 'PatientPhone',
    'cellphone': 'CellPhone',
    'email': 'Email',
}

_DATE_FORMATS = ('%m/%d/%Y', '%Y-%m-%d', '%m/%d/%y', '%Y-%m-%dT%H:%M:%S')


def _parse_date(value):
    """Best-effort parse of a Dutchie date string into a date; False on failure."""
    if not value:
        return False
    from datetime import datetime
    text = str(value).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text[:len(fmt) + 4], fmt).date()
        except (ValueError, TypeError):
            continue
    return False


class DutchieSyncCheckpoint(models.Model):
    _name = 'mint.dutchie.sync.checkpoint'
    _description = 'Dutchie Customer Roster Sync Checkpoint'
    _order = 'last_run asc, id asc'

    name = fields.Char(compute='_compute_name', store=True)
    company_id = fields.Many2one('res.company', string='Store', index=True)
    loc_id = fields.Char(string='Dutchie LocId', required=True, index=True, copy=False)
    state = fields.Selection(
        [('pending', 'Pending'), ('running', 'Running'),
         ('done', 'Done'), ('error', 'Error')],
        default='pending', index=True, copy=False,
    )
    page_size = fields.Integer(default=500)
    next_page = fields.Integer(string='Next Page (0-based)', default=0, copy=False)
    rows_done = fields.Integer(default=0, copy=False)
    rows_total = fields.Integer(default=0, copy=False)
    last_run = fields.Datetime(copy=False)
    last_error = fields.Text(copy=False)
    active = fields.Boolean(default=True)

    _loc_id_unique = models.Constraint(
        'UNIQUE(loc_id)', 'One sync checkpoint per Dutchie LocId.',
    )

    @api.depends('company_id', 'loc_id')
    def _compute_name(self):
        for rec in self:
            rec.name = '%s (%s)' % (rec.company_id.name or 'Store', rec.loc_id or '?')

    # ------------------------------------------------------------------
    # Cron entry — one bounded chunk per fire
    # ------------------------------------------------------------------
    @api.model
    def _cron_sync_roster(self):
        """Pick the most-stale unfinished store and process one page.

        Priority: stores still in pending/running/error (least-recently-run
        first). When every store is done, re-arm the most-stale done store so
        the roster stays current.
        """
        cp = self.search(
            [('active', '=', True), ('state', 'in', ('pending', 'running', 'error'))],
            order='last_run asc, id asc', limit=1,
        )
        if not cp:
            cp = self.search(
                [('active', '=', True), ('state', '=', 'done')],
                order='last_run asc, id asc', limit=1,
            )
            if not cp:
                return
            cp.write({'state': 'pending', 'next_page': 0})
        cp._run_chunk()

    def _run_chunk(self):
        self.ensure_one()
        cipher = get_cipher(self.env)
        if cipher is None:
            self.write({
                'state': 'error',
                'last_error': 'PII encryption key not configured '
                              '(DUTCHIE_PII_FERNET_KEY); refusing to import.',
                'last_run': fields.Datetime.now(),
            })
            return
        try:
            rows, has_more, total = self._fetch_roster_page(
                self.loc_id, self.next_page, self.page_size)
        except Exception as exc:  # network/502/timeout — record and retry next tick
            _logger.exception("Dutchie roster fetch failed for loc %s", self.loc_id)
            self.write({
                'state': 'error',
                'last_error': str(exc)[:2000],
                'last_run': fields.Datetime.now(),
            })
            return

        Partner = self.env['res.partner'].sudo()
        seen = set()
        processed = 0
        for row in rows:
            key = self._identity_key(row)
            if not key or key in seen:
                continue
            seen.add(key)
            self._upsert_partner(Partner, row, cipher)
            processed += 1

        vals = {
            'rows_done': self.rows_done + processed,
            'rows_total': total or self.rows_total,
            'last_run': fields.Datetime.now(),
            'last_error': False,
        }
        if has_more:
            vals.update(state='running', next_page=self.next_page + 1)
        else:
            vals.update(state='done')
        self.write(vals)

    # ------------------------------------------------------------------
    # Integration SEAM — step 2b
    # ------------------------------------------------------------------
    def _fetch_roster_page(self, loc_id, page, page_size):
        """Return ``(rows, has_more, total)`` for one page of report 125.

        ``rows`` is a list of dicts keyed by the report-125 column names in
        ROSTER_COLS. Implement against the proven Dutchie Backoffice client
        (mintinvsvc) — see step 2b. Must be retry-safe on 502/timeout (raising
        is fine; the caller records the error and retries on the next tick).
        """
        raise NotImplementedError(
            "Dutchie roster fetch not wired yet (step 2b). "
            "Implement _fetch_roster_page for loc_id=%s." % loc_id
        )

    # ------------------------------------------------------------------
    # Deterministic dedup + encrypted upsert (verified in V1)
    # ------------------------------------------------------------------
    @staticmethod
    def _identity_key(row):
        """Stable cross-store identity: DL -> MJStateID -> Name+DOB -> phone.

        report-125 ``Id`` is a PER-LOCATION row id (no cross-store overlap), so
        it is NOT a valid dedup key — use the identity attributes instead.
        """
        get = row.get
        dl = (get(ROSTER_COLS['dl']) or '').strip()
        if dl:
            return ('dl', dl.upper())
        mj = (get(ROSTER_COLS['mj_state_id']) or '').strip()
        if mj:
            return ('mj', mj.upper())
        name = (get(ROSTER_COLS['name']) or '').strip().upper()
        dob = (get(ROSTER_COLS['dob']) or '').strip()
        if name and dob:
            return ('namedob', name, dob)
        phone = (get(ROSTER_COLS['phone']) or get(ROSTER_COLS['cellphone']) or '').strip()
        if phone:
            return ('phone', phone)
        return None

    def _upsert_partner(self, Partner, row, cipher):
        """Create or update a res.partner from one report-125 row.

        Matches on x_dutchie_customer_id. PII (DOB/DL/MJ) is written through the
        encrypted plaintext fields, whose inverse encrypts via ``cipher``.
        """
        get = row.get
        cust_id = get(ROSTER_COLS['id'])
        if not cust_id:
            return
        cust_id = str(cust_id)

        vals = {
            'x_dutchie_customer_id': cust_id,
            'x_dutchie_patient_type': get(ROSTER_COLS['patient_type']) or False,
            'x_dutchie_patient_status': get(ROSTER_COLS['patient_status']) or False,
            'x_dutchie_gender': get(ROSTER_COLS['gender']) or False,
            'x_dutchie_mj_id_expiration': _parse_date(get(ROSTER_COLS['mj_expiration'])),
            'x_dutchie_member_since': _parse_date(get(ROSTER_COLS['member_since'])),
            'x_dutchie_last_sync': fields.Datetime.now(),
            # encrypted PII (routed through inverse)
            'x_dutchie_dob': (get(ROSTER_COLS['dob']) or '').strip() or False,
            'x_dutchie_dl': (get(ROSTER_COLS['dl']) or '').strip() or False,
            'x_dutchie_mj_state_id': (get(ROSTER_COLS['mj_state_id']) or '').strip() or False,
        }
        # Contact fields: only set when present, never blank out existing data.
        name = (get(ROSTER_COLS['name']) or '').strip()
        if name:
            vals['name'] = name
        for field_name, col in (('street', 'street'), ('city', 'city'), ('zip', 'zip')):
            value = (get(ROSTER_COLS[col]) or '').strip()
            if value:
                vals[field_name] = value
        phone = (get(ROSTER_COLS['phone']) or get(ROSTER_COLS['cellphone']) or '').strip()
        if phone:
            vals['phone'] = phone
        email = (get(ROSTER_COLS['email']) or '').strip()
        if email:
            vals['email'] = email
        if self.company_id:
            vals['x_home_store_id'] = self.company_id.id

        partner = Partner.search([('x_dutchie_customer_id', '=', cust_id)], limit=1)
        if partner:
            partner.write(vals)
        else:
            vals.setdefault('name', _('Dutchie Customer %s') % cust_id)
            vals['customer_rank'] = 1
            Partner.create(vals)
