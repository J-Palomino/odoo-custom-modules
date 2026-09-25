"""Pull vendor "Promo - Deal submission" entries from JotForm into
mint.deal.submission.

Python port of the FE repo's scripts/jotform/import-vendor-deals.mjs, so the
import can run server-side on a schedule instead of by hand (the manual script
went unrun from 2026-06-12 to 2026-09-23 and 412 submissions piled up).

The trigger is external: a daisy.plus Marketing agency POSTs to
/mint_cc/jotform/sync (controllers/jotform_sync.py) on a cron. There is no
ir.cron here on purpose — the schedule lives with the agency.

Same lossless contract as the Node importer:
  * jotform_payload keeps every answer verbatim (JSON);
  * known questions are ALSO mapped to structured columns;
  * free-text deal details are never parsed into invented values here (the
    model's create() auto-parse fills discount_value only when unambiguous).

Idempotent on (source='jotform', external_id), backed by the partial unique
index in deal_submission.init(). The fetch window is stateless: per form it
starts at the newest external_created_at already imported, minus a lookback,
so a missed or failed run is healed by the next one.
"""
import json
import logging
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from odoo import _, api, models
from odoo.exceptions import AccessError
from odoo.tools import html_escape

_logger = logging.getLogger(__name__)

JOTFORM_API = 'https://api.jotform.com'

PARAM_API_KEY = 'mint_cc.jotform.api_key'
PARAM_MODE = 'mint_cc.jotform.mode'                    # off | dry | live (default off)
PARAM_FORMS = 'mint_cc.jotform.forms'                  # JSON [{"code","form_id"}]; blank = DEFAULT_FORMS
PARAM_LOOKBACK_HOURS = 'mint_cc.jotform.lookback_hours'  # overlap re-scanned each run (default 24)
PARAM_REVIEWER_USER = 'mint_cc.jotform.reviewer_user_id'  # To-Do assignee; blank = Vendor Promos team leader
PARAM_CRM_LEAD = 'mint_cc.jotform.crm_lead'            # '0' disables the Vendor Promos CRM lead (default on)

# The active intake forms, one per market (region code on mint.region). The
# disabled legacy form 251327636084155 and the disabled IL duplicate are
# intentionally excluded.
DEFAULT_FORMS = [
    {'code': 'AZ', 'form_id': '253495306831157'},
    {'code': 'MO', 'form_id': '253495266582165'},
    {'code': 'NV', 'form_id': '253495133054152'},
    {'code': 'IL', 'form_id': '253495489327169'},
]

# First run with nothing imported for a form: how far back to look.
BOOTSTRAP_DAYS = 30
PAGE_SIZE = 100
TS_FMT = '%Y-%m-%d %H:%M:%S'

# Serialises overlapping triggers (two cron fires, a manual run). Arbitrary
# constant, scoped to this job.
_ADVISORY_LOCK_KEY = 7163201

DEAL_TYPE_MAP = {
    '% off': 'percent',
    'percent off': 'percent',
    '$ off': 'fixed',
    'dollar off': 'fixed',
    'bogo': 'bogo',
    'bundle': 'bundle',
    'set price': 'price',
    'price': 'price',
}

_RE_DATE = re.compile(r'(\d{4})-(\d{2})-(\d{2})')


def _norm(s):
    return re.sub(r'\s+', ' ', (s or '').lower()).strip()


def _flatten(answer):
    """JotForm answer (str | list | composite dict) -> plain text."""
    if answer is None:
        return ''
    if isinstance(answer, str):
        return answer.strip()
    if isinstance(answer, list):
        return '\n'.join(str(a) for a in answer if a).strip()
    if isinstance(answer, dict):
        return '\n'.join(str(v) for v in answer.values() if v not in (None, '')).strip()
    return str(answer).strip()


def _map_deal_type(raw):
    n = _norm(raw)
    if not n:
        return False
    if n in DEAL_TYPE_MAP:
        return DEAL_TYPE_MAP[n]
    for key, val in DEAL_TYPE_MAP.items():
        if key in n:
            return val
    return False


def _extract_date(raw):
    """First YYYY-MM-DD in a flattened date answer, e.g. the datetime widget's
    "10\\n01\\n2026\\n2026-10-01 00:00:00"."""
    m = _RE_DATE.search(raw or '')
    return '%s-%s-%s' % m.groups() if m else False


class MintDealSubmissionJotform(models.Model):
    _inherit = 'mint.deal.submission'

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------

    @api.model
    def _jotform_param(self, key, default=''):
        return self.env['ir.config_parameter'].sudo().get_param(key, default) or default

    @api.model
    def _jotform_forms(self):
        raw = self._jotform_param(PARAM_FORMS)
        if not raw:
            return DEFAULT_FORMS
        try:
            forms = json.loads(raw)
            return [f for f in forms if f.get('code') and f.get('form_id')]
        except (ValueError, AttributeError):
            _logger.warning('jotform_sync: %s is not valid JSON — using defaults', PARAM_FORMS)
            return DEFAULT_FORMS

    # ------------------------------------------------------------------
    # JotForm API
    # ------------------------------------------------------------------

    @api.model
    def _jotform_get(self, path, params, api_key):
        """GET a JotForm endpoint and return `content`. The key travels in the
        APIKEY header, not the query string, so it never lands in URL logs."""
        url = '%s%s?%s' % (JOTFORM_API, path, urllib.parse.urlencode(params))
        req = urllib.request.Request(url, headers={
            'APIKEY': api_key,
            'User-Agent': 'mint-odoo-jotform-sync/1.0',
        })
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode('utf-8', 'replace'))
        if data.get('responseCode') != 200:
            raise RuntimeError('JotForm %s: %s %s' % (
                path, data.get('responseCode'), data.get('message')))
        return data.get('content') or []

    @api.model
    def _jotform_fetch_since(self, form_id, since, api_key):
        """Every non-deleted submission on a form created after `since`
        (JotForm account-local 'YYYY-MM-DD HH:MM:SS'), oldest first."""
        out = []
        offset = 0
        while True:
            page = self._jotform_get('/form/%s/submissions' % form_id, {
                'limit': PAGE_SIZE,
                'offset': offset,
                'orderby': 'created_at',
                'filter': json.dumps({'created_at:gt': since}),
            }, api_key)
            out.extend(page)
            if len(page) < PAGE_SIZE:
                break
            offset += PAGE_SIZE
        subs = [s for s in out
                if s.get('status') != 'DELETED' and (s.get('created_at') or '') > since]
        subs.sort(key=lambda s: s.get('created_at') or '')
        return subs

    @api.model
    def _jotform_window_start(self, form_id, lookback_hours):
        """Start of the fetch window for one form: newest imported
        external_created_at minus the lookback, or BOOTSTRAP_DAYS ago."""
        latest = self.sudo().search([
            ('source', '=', 'jotform'),
            ('external_form_id', '=', form_id),
            ('external_created_at', '!=', False),
        ], order='external_created_at desc', limit=1)
        try:
            anchor = datetime.strptime(latest.external_created_at, TS_FMT) if latest else None
        except (TypeError, ValueError):
            anchor = None
        if anchor is None:
            anchor = datetime.now() - timedelta(days=BOOTSTRAP_DAYS)
        return (anchor - timedelta(hours=lookback_hours)).strftime(TS_FMT)

    # ------------------------------------------------------------------
    # Mapping
    # ------------------------------------------------------------------

    @api.model
    def _jotform_build_vals(self, sub, form_code, form_id):
        """One JotForm submission -> mint.deal.submission vals. Label-matched
        (not qid-matched): the four market forms share question text but not
        question ids."""
        payload_fields = []
        labeled = {}
        for qid, ans in (sub.get('answers') or {}).items():
            label = ans.get('text') or ans.get('name') or qid
            flat = _flatten(ans.get('answer'))
            payload_fields.append({
                'qid': qid,
                'name': ans.get('name'),
                'label': label,
                'answer': ans.get('answer'),
                'text': flat,
            })
            if flat:
                labeled[_norm(label)] = flat

        payload = {
            'jotform_submission_id': sub.get('id'),
            'form_id': form_id,
            'form_code': form_code,
            'created_at': sub.get('created_at'),
            'status': sub.get('status'),
            'ip': sub.get('ip'),
            'fields': payload_fields,
        }

        def get(*prefixes):
            for label, text in labeled.items():
                if any(label.startswith(p) for p in prefixes):
                    return text
            return ''

        brand = get('brand')
        frequency = get('deal frequency')
        deal_type_raw = get('deal type')
        contact = re.sub(r'\s+', ' ', get('submitted by')).strip()
        promo_qty = re.sub(r'[^\d]', '', get('please let us know the quantity you will be sending'))
        end_note = get('if this is not an edlp')

        name = ' '.join(p for p in [
            brand or contact or 'Vendor Deal',
            deal_type_raw and '— %s' % deal_type_raw,
            frequency and '(%s)' % frequency,
        ] if p)[:120]

        return {
            'source': 'jotform',
            'external_id': str(sub.get('id')),
            'external_form_id': form_id,
            'external_created_at': sub.get('created_at') or False,
            'jotform_payload': json.dumps(payload, indent=2, default=str),

            'vendor_name': brand or contact or 'Unknown Vendor',
            'vendor_email': get('best contact email', 'best email') or False,
            'vendor_contact': contact or False,
            'vendor_phone': get('best phone') or False,

            'name': name,
            'discount_type': _map_deal_type(deal_type_raw),
            'deal_frequency': frequency or False,
            'product_list': get('product(s)') or False,
            'sales_details': get('please give us the deal details') or False,
            'excluded_skus': get('please let us know if there are any skus this deal excludes') or False,
            'reviewer_notes': get('notes') or False,
            'deal_end_note': end_note[:250] if end_note else False,

            'promo_units': get('are you able to provide promo units') or False,
            'promo_units_product': get('please let us know what product you will be sending') or False,
            'promo_units_qty': int(promo_qty) if promo_qty else 0,
            'promo_delivery_date': _extract_date(get('estimated delivery date')),

            # Newer form revisions ask for structured dates. create() promotes
            # these into a mint.deal.submission.window (_sync_legacy_window).
            'preferred_start_date': _extract_date(get('suggested start date')),
            'preferred_end_date': _extract_date(get('end date')),
        }

    @api.model
    def _jotform_market_vals(self, region):
        """market_id + Requested Stores, mirroring _onchange_market_fill_stores
        and the /vendor-deals controller (live dispensaries only)."""
        if not region:
            return {}
        vals = {'market_id': region.id}
        stores = region.store_ids.filtered(
            lambda c: getattr(c, 'is_dispensary', False) and getattr(c, 'is_active', True)
        )
        if stores:
            vals['store_ids'] = [(6, 0, stores.ids)]
        return vals

    @api.model
    def _jotform_vendor_promos_team(self):
        """The CRM team vendor deals are worked from — same lookup as the
        /vendor-deals controller."""
        return self.env.ref('sales_team.salesteam_vendor_promos', raise_if_not_found=False) \
            or self.env['crm.team'].sudo().search([('name', '=', 'Vendor Promos')], limit=1)

    def _jotform_create_crm_lead(self, team, form_code):
        """Mirror this submission as a Vendor Promos opportunity and link it,
        the way the /vendor-deals controller does for web-form submissions, so
        marketing works JotForm and web deals from one CRM pipeline."""
        self.ensure_one()
        body = [
            '<p><strong>Imported from JotForm</strong> (%s form, submission %s, submitted %s).</p>'
            % (html_escape(form_code), html_escape(self.external_id or ''),
               html_escape(self.external_created_at or '')),
        ]
        for label, value in (('Deal details', self.sales_details),
                             ('Products', self.product_list),
                             ('Frequency', self.deal_frequency),
                             ('Excluded SKUs', self.excluded_skus)):
            if value:
                body.append('<p><strong>%s:</strong><br/>%s</p>' % (
                    label, html_escape(value).replace('\n', '<br/>')))
        vals = {
            'name': ('Promo - %s: %s' % (self.vendor_name, self.name))[:250],
            'type': 'opportunity',
            'contact_name': self.vendor_contact or self.vendor_name,
            'partner_name': self.vendor_name,
            'email_from': self.vendor_email or False,
            'phone': self.vendor_phone or False,
            'vendor_brand_id': self.brand_id.id or False,
            'description': ''.join(body),
        }
        if team:
            vals['team_id'] = team.id
            if team.user_id:
                vals['user_id'] = team.user_id.id
        lead = self.env['crm.lead'].sudo().create(vals)
        self.sudo().write({'crm_lead_id': lead.id})
        return lead

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    @api.model
    def action_jotform_sync(self, dry_run=None):
        """RPC/manual entry point. Gated to vendor managers and admins so a
        read-only user cannot drive imports; the body runs sudo."""
        if not (self.env.su
                or self.env.user.has_group('base.group_system')
                or self.env.user.has_group('mint_command_center.group_vendor_manager')):
            raise AccessError(_('Only vendor managers can run the JotForm sync.'))
        return self._jotform_sync(dry_run=dry_run)

    @api.model
    def _jotform_sync(self, dry_run=None):
        """Import new JotForm submissions. Returns a JSON-able summary.

        Mode comes from mint_cc.jotform.mode (off | dry | live); `dry_run=True`
        forces a dry pass regardless. Per-form and per-row failures are logged
        and counted, never raised, so one bad form cannot stall the others.
        """
        started = time.monotonic()
        mode = (self._jotform_param(PARAM_MODE, 'off') or 'off').strip().lower()
        if dry_run and mode != 'off':
            mode = 'dry'
        summary = {'mode': mode, 'forms': {}, 'fetched': 0, 'created': 0,
                   'would_create': 0, 'skipped_existing': 0, 'errors': 0,
                   'error_messages': []}
        if mode not in ('dry', 'live'):
            summary['status'] = 'disabled'
            return summary

        api_key = self._jotform_param(PARAM_API_KEY)
        if not api_key:
            _logger.warning('jotform_sync: %s unset — skipped', PARAM_API_KEY)
            summary['status'] = 'no_api_key'
            return summary

        self.env.cr.execute('SELECT pg_try_advisory_xact_lock(%s)', (_ADVISORY_LOCK_KEY,))
        if not self.env.cr.fetchone()[0]:
            summary['status'] = 'already_running'
            return summary

        try:
            lookback = int(self._jotform_param(PARAM_LOOKBACK_HOURS, '24'))
        except ValueError:
            lookback = 24
        team = self._jotform_vendor_promos_team()
        make_lead = self._jotform_param(PARAM_CRM_LEAD, '1').strip() not in ('0', 'false', 'no', 'off')
        reviewer = False
        reviewer_id = self._jotform_param(PARAM_REVIEWER_USER)
        if reviewer_id and reviewer_id.isdigit():
            reviewer = self.env['res.users'].sudo().browse(int(reviewer_id)).exists()
        if not reviewer and team:
            reviewer = team.user_id
        summary['leads_created'] = 0
        summary['todos_created'] = 0

        Sub = self.sudo()
        Region = self.env['mint.region'].sudo()

        def _err(msg):
            summary['errors'] += 1
            if len(summary['error_messages']) < 10:
                summary['error_messages'].append(msg)
            _logger.warning('jotform_sync: %s', msg)

        for form in self._jotform_forms():
            code, form_id = form['code'], str(form['form_id'])
            fs = summary['forms'][code] = {'fetched': 0, 'created': 0,
                                           'skipped_existing': 0, 'errors': 0}
            since = self._jotform_window_start(form_id, lookback)
            fs['since'] = since
            try:
                subs = self._jotform_fetch_since(form_id, since, api_key)
            except Exception as e:  # network, HTTP, JSON — skip this form only
                fs['errors'] += 1
                _err('%s fetch failed: %s' % (code, e))
                continue
            fs['fetched'] = len(subs)
            summary['fetched'] += len(subs)
            if not subs:
                continue

            existing = set(Sub.search([
                ('source', '=', 'jotform'),
                ('external_id', 'in', [str(s.get('id')) for s in subs]),
            ]).mapped('external_id'))
            region = Region.search([('code', '=', code)], limit=1)
            market_vals = self._jotform_market_vals(region)

            for sub in subs:
                ext_id = str(sub.get('id'))
                if ext_id in existing:
                    fs['skipped_existing'] += 1
                    summary['skipped_existing'] += 1
                    continue
                try:
                    vals = self._jotform_build_vals(sub, code, form_id)
                    vals.update(market_vals)
                except Exception as e:
                    fs['errors'] += 1
                    _err('%s sub %s mapping failed: %s' % (code, ext_id, e))
                    continue
                if mode == 'dry':
                    summary['would_create'] += 1
                    continue
                try:
                    # Savepoint per row: a unique-index hit (a concurrent
                    # importer beat us) or any constraint rolls back this row
                    # only, not the batch.
                    with self.env.cr.savepoint():
                        rec = Sub.create(vals)
                    fs['created'] += 1
                    summary['created'] += 1
                except Exception as e:
                    if re.search(r'unique|duplicate', str(e), re.I):
                        fs['skipped_existing'] += 1
                        summary['skipped_existing'] += 1
                    else:
                        fs['errors'] += 1
                        _err('%s sub %s create failed: %s' % (code, ext_id, e))
                    continue

                # Follow-ups each get their own savepoint: the submission is
                # already safe, so a CRM or activity hiccup must not undo it
                # (same stance as the /vendor-deals controller).
                if make_lead:
                    try:
                        with self.env.cr.savepoint():
                            rec._jotform_create_crm_lead(team, code)
                        summary['leads_created'] += 1
                    except Exception as e:
                        fs['errors'] += 1
                        _err('%s sub %s saved, CRM lead failed: %s' % (code, ext_id, e))
                if reviewer:
                    try:
                        with self.env.cr.savepoint():
                            rec.activity_schedule(
                                'mail.mail_activity_data_todo',
                                user_id=reviewer.id,
                                summary='Review JotForm deal submission',
                                note='New %s vendor deal from %s via JotForm.' % (
                                    code, vals['vendor_name']),
                            )
                        summary['todos_created'] += 1
                    except Exception as e:
                        fs['errors'] += 1
                        _err('%s sub %s saved, To-Do failed: %s' % (code, ext_id, e))

        summary['status'] = 'ok' if not summary['errors'] else 'partial'
        summary['duration_s'] = round(time.monotonic() - started, 2)
        _logger.info('jotform_sync: %s', {k: v for k, v in summary.items()
                                          if k != 'error_messages'})
        return summary
