# -*- coding: utf-8 -*-
"""
Resumable per-store checkpoint + cron for backfilling Dutchie customer rosters
into res.partner.

Each Dutchie store (one LocId) gets one ``mint.dutchie.sync.checkpoint`` row.
The cron processes ONE WHOLE STORE per fire: it streams that store's Patient
Contact Report (ReportId 125) from the mintinvsvc endpoint as NDJSON and upserts
the rows in savepoint batches, then marks the store done. Report 125 has no
server-side paging and returns the entire roster in one snapshot, so a single
streamed request per store is the coherent unit of work (no offset cursor, no
per-process cache, no cross-instance paging hazard). The per-store fetch is the
only fragile call (Dutchie 502/timeout); on failure the store goes to 'error'
and is retried on the next tick.

⚠️ Known limitation: a large store (e.g. Tempe ~365K rows) is one cron fire that
holds its checkpoint row's FOR UPDATE lock and an OPEN transaction for the whole
multi-minute stream. On the 2-worker prod Odoo this can pin a worker and lag
autovacuum for that window, so run the initial backfill OFF-HOURS. Bounding the
transaction via per-batch commits is deferred: committing mid-cron can release
the ir.cron job lock in Odoo, so that change needs validation against v19
cron-lock semantics before it is made.

Identity model
--------------
report-125 ``Id`` is a PER-LOCATION row id (no cross-store overlap), so it is
NOT a valid dedup key. The same human at N stores has N different ``Id`` values.
Dedup + upsert therefore key on a normalized identity (``x_dutchie_identity_key``
= DL > MJ state ID > Name+DOB > phone), matched via a DB search so dedup works
across batches AND stores. ``x_dutchie_customer_id`` is recorded first-seen only
(never churned) as a reference.

  ⚠️ OPEN: whether Dutchie exposes a stable GLOBAL customer id (vs the
  per-location report-125 ``Id``), and how this reconciles with mint_pos_bridge's
  existing match-on-customer_id flow, should be confirmed with a live
  get-customers call. Until then the identity hash is authoritative.

Column names
------------
ROSTER_COLS keys are confirmed VERBATIM against a live report-125 response
(Riverview, 2026-06-24). Two keys contain spaces and are bracket-accessed.
"""
import json
import logging

import requests

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .pii_crypto import encrypt_value, get_cipher

_logger = logging.getLogger(__name__)

# mintinvsvc endpoint config. Reuse the module's established api-key param
# (mint.inventory_service.api_key, shared with product_sync). Default base URL
# is the non-deprecated (no-suffix) inventory service.
INVSVC_URL_PARAM = 'mint.dutchie_sync.invsvc_url'
DEFAULT_INVSVC_URL = 'https://mintinvsvc-production.up.railway.app'
INVSVC_API_KEY_PARAM = 'mint.inventory_service.api_key'

# Patient Contact Report (ReportId 125) column -> handling. ``Id`` is a
# per-location row PK (no cross-store overlap) — a reference id, NOT the dedup key.
ROSTER_COLS = {
    'id': 'Id',
    'name': 'Accts_Name',
    'dob': 'PatientDOB',             # -> x_dutchie_dob (encrypted)
    'dl': 'DriversLicense',          # -> x_dutchie_dl (encrypted)
    'mj_state_id': 'MJStateIDNo',    # -> x_dutchie_mj_state_id (encrypted)
    'mj_expiration': 'MMJ ID Expiration Date',
    'patient_type': 'patientType',
    'patient_status': 'PatientStatus',
    'gender': 'Gender',
    'member_since': 'Member Since',
    'street': 'Accts_Addr1',
    'city': 'Accts_City',
    'zip': 'PostalCode',
    'phone': 'PatientPhone',
    'cellphone': 'CellPhone',
    'email': 'Email',
}


def _parse_date(value):
    """Best-effort parse of a Dutchie date string into a date; False on failure.

    Dutchie returns empty datetimes as a year-1 sentinel (0001-01-01...); any
    parsed year <= 1 is treated as no value.
    """
    if not value:
        return False
    from datetime import datetime
    text = str(value).strip().split('T')[0].split(' ')[0]
    for fmt in ('%m/%d/%Y', '%Y-%m-%d', '%m/%d/%y'):
        try:
            parsed = datetime.strptime(text, fmt).date()
        except (ValueError, TypeError):
            continue
        return False if parsed.year <= 1 else parsed
    return False


class DutchieSyncCheckpoint(models.Model):
    _name = 'mint.dutchie.sync.checkpoint'
    _description = 'Dutchie Customer Roster Sync Checkpoint'
    _order = 'last_run asc, id asc'

    name = fields.Char(compute='_compute_name', store=True)
    company_id = fields.Many2one('res.company', string='Store', index=True)
    loc_id = fields.Char(string='Dutchie LocId', required=True, index=True, copy=False)
    lsp_id = fields.Char(
        string='Dutchie LspId', copy=False,
        help='Per-store Dutchie LSP id, required by the report-125 run-report call.',
    )
    state = fields.Selection(
        [('pending', 'Pending'), ('running', 'Running'),
         ('done', 'Done'), ('error', 'Error')],
        default='pending', index=True, copy=False,
    )
    batch_size = fields.Integer(
        string='Upsert Batch Size', default=500,
        help='Rows upserted per savepoint batch while streaming a store roster.',
    )
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
    # Cron entry — claim one store atomically, sync it fully
    # ------------------------------------------------------------------
    @api.model
    def _cron_sync_roster(self):
        """Claim the most-stale unfinished store and sync it.

        Uses SELECT ... FOR UPDATE SKIP LOCKED so two overlapping fires (or
        workers) never process the same checkpoint: an actively-running row is
        row-locked and skipped, while a row left 'running' by a crash has its
        lock released and is reclaimable. NULLS FIRST so never-run stores are
        picked before already-run ones.
        """
        table = self._table
        self.env.cr.execute(
            "SELECT id FROM %s "
            "WHERE active = TRUE AND state IN ('pending', 'running', 'error') "
            "ORDER BY last_run ASC NULLS FIRST, id ASC "
            "LIMIT 1 FOR UPDATE SKIP LOCKED" % table
        )
        res = self.env.cr.fetchone()
        if res:
            cp = self.browse(res[0])
        else:
            # All stores done → re-arm the stalest for a refresh pass.
            self.env.cr.execute(
                "SELECT id FROM %s "
                "WHERE active = TRUE AND state = 'done' "
                "ORDER BY last_run ASC NULLS FIRST, id ASC "
                "LIMIT 1 FOR UPDATE SKIP LOCKED" % table
            )
            res = self.env.cr.fetchone()
            if not res:
                return
            cp = self.browse(res[0])
            cp.write({'state': 'pending', 'rows_done': 0})
        cp.state = 'running'  # cosmetic; the row lock above is the real guard
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
            total, processed = self._sync_store(cipher)
        except Exception as exc:  # network/502/timeout — record and retry next tick
            _logger.exception("Dutchie roster sync failed for loc %s", self.loc_id)
            self.write({
                'state': 'error',
                'last_error': str(exc)[:2000],
                'last_run': fields.Datetime.now(),
            })
            return
        self.write({
            'state': 'done',
            'rows_total': total,
            'rows_done': processed,
            'last_run': fields.Datetime.now(),
            'last_error': False,
        })

    # ------------------------------------------------------------------
    # Fetch — stream the whole store roster as NDJSON, upsert in batches
    # ------------------------------------------------------------------
    def _sync_store(self, cipher):
        """Stream report-125 rows for this store and upsert them in batches.

        Returns ``(total, processed)``. The mintinvsvc endpoint runs report 125
        once and streams one JSON object per line; we buffer ``batch_size`` rows,
        upsert them, then flush+invalidate the ORM cache so peak memory stays
        bounded even for 365K-row stores. Flush (not commit) keeps the row lock.
        """
        ICP = self.env['ir.config_parameter'].sudo()
        base = (ICP.get_param(INVSVC_URL_PARAM, DEFAULT_INVSVC_URL) or '').rstrip('/')
        api_key = ICP.get_param(INVSVC_API_KEY_PARAM) or ''
        if not base:
            raise UserError(_("Inventory-service URL not configured (%s).") % INVSVC_URL_PARAM)
        if not self.lsp_id:
            raise UserError(_("Checkpoint for LocId %s has no LspId set.") % self.loc_id)

        batch_size = self.batch_size or 500
        processed = 0
        seen = 0
        batch = []
        with requests.get(
            base + '/dutchie/customer-roster',
            params={'locId': self.loc_id, 'lspId': self.lsp_id},
            headers={'X-Api-Key': api_key},
            stream=True,
            timeout=(30, 600),
        ) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get('X-Total-Count') or 0)
            for line in resp.iter_lines(decode_unicode=True):
                if not line:
                    continue
                seen += 1
                batch.append(json.loads(line))
                if len(batch) >= batch_size:
                    processed += self._upsert_rows(batch, cipher)
                    batch = []
                    # Persist this batch and release ORM cache memory (no commit
                    # → the FOR UPDATE checkpoint lock is retained for this store).
                    self.env.cr.flush()
                    self.env.invalidate_all()
            if batch:
                processed += self._upsert_rows(batch, cipher)
        # Integrity: a stream truncated on a clean newline boundary parses fine
        # but drops the tail. Refuse to mark 'done' if fewer rows arrived than the
        # endpoint promised — raise so the store is retried in full next tick.
        if total and seen < total:
            raise ValueError(
                "Roster stream truncated for loc %s: received %d of %d rows."
                % (self.loc_id, seen, total))
        # rows_total = raw rows streamed (seen); rows_done = upserted-after-dedup
        # (processed). They legitimately differ by identity-key dedup attrition.
        return (total or seen), processed

    # ------------------------------------------------------------------
    # Deterministic dedup + batched encrypted upsert
    # ------------------------------------------------------------------
    @staticmethod
    def _identity_key(row):
        """Stable cross-store identity string: DL > MJ state id > Name+DOB > phone.

        Returns None for rows with no stable identifier (skipped, not imported).
        """
        get = row.get
        dl = (get(ROSTER_COLS['dl']) or '').strip()
        if dl:
            return 'dl:' + dl.upper()
        mj = (get(ROSTER_COLS['mj_state_id']) or '').strip()
        if mj:
            return 'mj:' + mj.upper()
        name = (get(ROSTER_COLS['name']) or '').strip().upper()
        dob = (get(ROSTER_COLS['dob']) or '').strip()
        if name and dob:
            return 'nd:%s|%s' % (name, dob)
        phone = (get(ROSTER_COLS['phone']) or get(ROSTER_COLS['cellphone']) or '').strip()
        if phone:
            return 'ph:' + phone
        return None

    def _upsert_rows(self, rows, cipher):
        """Batch-upsert a slice of report-125 rows; return processed count.

        Dedups within the batch by identity (later row wins), then resolves
        existing partners in TWO bulk searches (by identity key, and by
        per-location customer id as a fallback so the ~83K already-linked
        partners are matched and back-filled rather than duplicated). Cross-batch
        dedup works because the caller flushes each batch to the DB before the
        next batch's search runs. Each write/create is wrapped in a savepoint so
        one bad row (e.g. a UNIQUE customer_id collision) cannot abort the batch.
        """
        Partner = self.env['res.partner'].sudo()
        keyed = {}
        for row in rows:
            key = self._identity_key(row)
            if key:
                keyed[key] = row  # in-batch dedup, later wins
        if not keyed:
            return 0

        keys = list(keyed)
        cust_ids = [
            str(row.get(ROSTER_COLS['id']))
            for row in keyed.values() if row.get(ROSTER_COLS['id'])
        ]
        by_identity = {
            p['x_dutchie_identity_key']: p['id']
            for p in Partner.search_read(
                [('x_dutchie_identity_key', 'in', keys)], ['id', 'x_dutchie_identity_key'])
        }
        by_custid = {}
        if cust_ids:
            by_custid = {
                p['x_dutchie_customer_id']: p['id']
                for p in Partner.search_read(
                    [('x_dutchie_customer_id', 'in', cust_ids)], ['id', 'x_dutchie_customer_id'])
            }

        processed = 0
        for key, row in keyed.items():
            cust_id = str(row.get(ROSTER_COLS['id']) or '') or False
            pid = by_identity.get(key) or (by_custid.get(cust_id) if cust_id else None)
            try:
                with self.env.cr.savepoint():
                    if pid:
                        Partner.browse(pid).write(self._row_to_vals(row, key, cipher, False))
                    else:
                        new = Partner.create(self._row_to_vals(row, key, cipher, True))
                        by_identity[key] = new.id
                        if cust_id:
                            by_custid[cust_id] = new.id
                processed += 1
            except Exception:
                _logger.exception(
                    "Roster upsert failed (loc %s, identity %s)", self.loc_id, key)
        return processed

    def _row_to_vals(self, row, identity_key, cipher, is_create):
        """Map a report-125 row to res.partner vals.

        PII is written directly to the encrypted ``*_enc`` columns using the
        store-resolved ``cipher`` (one encrypt per non-empty field, no per-record
        cipher rebuild / decrypt — the inverse path is bypassed for bulk).
        Contact fields are only set when present, never blanking existing data.
        ``x_dutchie_customer_id`` is set on CREATE only (first-seen, never churned).
        """
        get = row.get
        vals = {
            'x_dutchie_identity_key': identity_key,
            'x_dutchie_patient_type': get(ROSTER_COLS['patient_type']) or False,
            'x_dutchie_patient_status': get(ROSTER_COLS['patient_status']) or False,
            'x_dutchie_gender': get(ROSTER_COLS['gender']) or False,
            'x_dutchie_mj_id_expiration': _parse_date(get(ROSTER_COLS['mj_expiration'])),
            'x_dutchie_member_since': _parse_date(get(ROSTER_COLS['member_since'])),
            'x_dutchie_last_sync': fields.Datetime.now(),
            'x_dutchie_dob_enc': encrypt_value(
                self.env, (get(ROSTER_COLS['dob']) or '').strip(), cipher=cipher),
            'x_dutchie_dl_enc': encrypt_value(
                self.env, (get(ROSTER_COLS['dl']) or '').strip(), cipher=cipher),
            'x_dutchie_mj_state_id_enc': encrypt_value(
                self.env, (get(ROSTER_COLS['mj_state_id']) or '').strip(), cipher=cipher),
        }
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
        if is_create:
            cust_id = str(get(ROSTER_COLS['id']) or '') or False
            if cust_id:
                vals['x_dutchie_customer_id'] = cust_id
            vals.setdefault('name', _('Dutchie Customer %s') % (cust_id or identity_key))
            vals['customer_rank'] = 1
        return vals
