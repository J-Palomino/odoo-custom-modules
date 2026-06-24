# -*- coding: utf-8 -*-
"""
Resumable per-store checkpoint + cron for backfilling Dutchie customer rosters
into res.partner.

Each Dutchie store (one LocId) gets one ``mint.dutchie.sync.checkpoint`` row
tracking progress. The cron processes ONE bounded chunk per fire (one page of
one store), advances the checkpoint, and stops — so the multi-million-row
roster imports persistently across many short, retry-safe ticks rather than one
long call that would hit Dutchie's export 502/timeout limits.

Identity model
--------------
report-125 ``Id`` is a PER-LOCATION row id (no cross-store overlap), so it is
NOT a valid dedup key. The same human at N stores has N different ``Id`` values.
Dedup + upsert therefore key on a normalized identity (``x_dutchie_identity_key``
= DL > MJ state ID > Name+DOB > phone), matched via a DB search so dedup works
across pages AND stores — not just within one page. ``x_dutchie_customer_id`` is
recorded first-seen only (never churned) as a reference.

  ⚠️ OPEN (step 2b): whether Dutchie exposes a stable GLOBAL customer id (vs the
  per-location report-125 ``Id``), and how this reconciles with mint_pos_bridge's
  existing match-on-customer_id flow, must be confirmed with a live get-customers
  call before this is wired live. Until then the identity hash is authoritative.

Column names
------------
The ROSTER_COLS keys below are PROVISIONAL and MUST be confirmed verbatim against
a live report-125 response when ``_fetch_roster_page`` is implemented in step 2b
(CLAUDE.md: verify external API field names from a real call; never rely on
analogy). A mismatched key makes ``row.get`` return None and silently imports
blank data.
"""
import logging

from odoo import _, api, fields, models

from .pii_crypto import encrypt_value, get_cipher

_logger = logging.getLogger(__name__)

# Patient Contact Report (ReportId 125) column -> handling. Keys confirmed
# VERBATIM against a live run-report response (Riverview, 2026-06-24). Two keys
# contain spaces and must be bracket-accessed. ``Id`` is a per-location row PK
# (no cross-store overlap) — used as a reference id only, NOT the dedup key.
ROSTER_COLS = {
    'id': 'Id',                      # -> x_dutchie_customer_id (per-location id)
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

# Dutchie returns empty datetimes as this sentinel rather than null/blank.
_EMPTY_DATE_SENTINELS = ('0001-01-01', '1/1/0001', '01/01/0001')


def _parse_date(value):
    """Best-effort parse of a Dutchie date string into a date; False on failure.

    Treats Dutchie's empty-datetime sentinel (0001-01-01...) as no value.
    """
    if not value:
        return False
    from datetime import datetime
    raw = str(value).strip()
    if any(raw.startswith(s) for s in _EMPTY_DATE_SENTINELS):
        return False
    # Drop any time component, then match the whole remaining token exactly.
    text = raw.split('T')[0].split(' ')[0]
    for fmt in ('%m/%d/%Y', '%Y-%m-%d', '%m/%d/%y'):
        try:
            return datetime.strptime(text, fmt).date()
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
    lsp_id = fields.Char(
        string='Dutchie LspId', copy=False,
        help='Per-store Dutchie LSP id, required by the report-125 run-report call.',
    )
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
    # Cron entry — claim one store atomically, process one page
    # ------------------------------------------------------------------
    @api.model
    def _cron_sync_roster(self):
        """Claim the most-stale unfinished store and process one page.

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
            cp.write({'state': 'pending', 'next_page': 0, 'rows_done': 0})
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

        processed = self._upsert_rows(rows, cipher)

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

        Calls the mintinvsvc customer-roster endpoint, which runs report 125 via
        the proven Dutchie Backoffice client, caches the per-store roster
        (sorted by Id for stable offset paging), and serves offset/limit slices.
        ``rows`` are dicts keyed by the ROSTER_COLS report-125 column names.
        Raising on failure is fine — the caller records the error and retries.
        """
        import requests

        ICP = self.env['ir.config_parameter'].sudo()
        base = (ICP.get_param('mint_dutchie_sync.invsvc_url') or '').rstrip('/')
        api_key = ICP.get_param('mint_dutchie_sync.invsvc_api_key') or ''
        if not base:
            raise UserError(_(
                "Inventory-service URL not configured "
                "(mint_dutchie_sync.invsvc_url)."))
        offset = page * page_size
        resp = requests.get(
            base + '/dutchie/customer-roster',
            params={
                'locId': loc_id,
                'lspId': self.lsp_id or '',
                'offset': offset,
                'limit': page_size,
            },
            headers={'X-Api-Key': api_key},
            timeout=180,
        )
        resp.raise_for_status()
        payload = resp.json()
        rows = payload.get('rows') or []
        total = payload.get('total') or 0
        has_more = bool(payload.get('hasMore'))
        return rows, has_more, total

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
        """Batch-upsert one page of report-125 rows; return processed count.

        Dedups within the page by identity (later row wins), then resolves
        existing partners in TWO bulk searches (by identity key, and by
        per-location customer id as a fallback so the ~83K already-linked
        partners are matched and back-filled rather than duplicated). Each row's
        write/create is wrapped in a savepoint so one bad row (e.g. a UNIQUE
        customer_id collision) cannot abort the whole chunk.
        """
        Partner = self.env['res.partner'].sudo()
        keyed = {}
        for row in rows:
            key = self._identity_key(row)
            if key:
                keyed[key] = row  # in-page dedup, later wins
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
        page-resolved ``cipher`` (one encrypt per non-empty field, no per-record
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
