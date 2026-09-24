"""JotForm vendor-deal sync (models/jotform_sync.py).

The manual Node importer went unrun from 2026-06-12 to 2026-09-23 and 412
vendor submissions never reached Odoo. These lock in the server-side
replacement: lossless mapping, idempotency on external_id, mode gating, and
that one failing form cannot stall the others.
"""
import json
from unittest.mock import patch

from odoo.exceptions import AccessError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from ..models import jotform_sync as js


def _ans(text, answer, type_='control_textbox'):
    return {'text': text, 'name': text.split(':')[0][:20], 'type': type_, 'answer': answer}


def _sub(sub_id, created_at, brand='Test Brand', details='30% off all carts'):
    return {
        'id': sub_id,
        'created_at': created_at,
        'status': 'ACTIVE',
        'ip': '127.0.0.1',
        'answers': {
            '1': _ans('Brand', brand),
            '2': _ans('Deal Type', '% Off', 'control_dropdown'),
            '3': _ans('Deal frequency', 'Weekly', 'control_dropdown'),
            '4': _ans('Submitted by:', {'first': 'Pat', 'last': 'Vendor'}, 'control_fullname'),
            '5': _ans('Best contact Email', 'pat@vendor.test', 'control_email'),
            '6': _ans('Please give us the deal details below. Ex: 2 for $45', details,
                      'control_textarea'),
            '7': _ans('Please let us know the quantity you will be sending.', '12 units',
                      'control_number'),
            '8': _ans('Suggested Start Date ( we will Activate the promo ASAP )',
                      {'month': '10', 'day': '01', 'year': '2026',
                       'datetime': '2026-10-01 00:00:00'}, 'control_datetime'),
            '9': _ans('End Date', {'month': '10', 'day': '31', 'year': '2026',
                                   'datetime': '2026-10-31 00:00:00'}, 'control_datetime'),
            '10': _ans('Please let us know if there are any Skus this deal Excludes', 'SKU-1'),
            '11': _ans('Type a question', 'unmapped but kept'),
            '12': _ans('Submit', None, 'control_button'),
        },
    }


@tagged('post_install', '-at_install')
class TestJotformSync(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Sub = cls.env['mint.deal.submission']
        cls.region = cls.env['mint.region'].create({'name': 'Jot Test', 'code': 'ZT'})
        cls.store = cls.env['res.company'].create({
            'name': 'Jot Test Store', 'region_id': cls.region.id,
            'is_dispensary': True,
        })
        ICP = cls.env['ir.config_parameter'].sudo()
        ICP.set_param(js.PARAM_API_KEY, 'test-key')
        ICP.set_param(js.PARAM_MODE, 'live')
        ICP.set_param(js.PARAM_FORMS, json.dumps([
            {'code': 'ZT', 'form_id': 'F1'},
            {'code': 'ZZ', 'form_id': 'F2'},
        ]))
        # Vendor Promos team, led by an existing user (creating res.users in
        # this module's tests costs ~30s each — see the staging test traps).
        cls.leader = cls.env.ref('base.user_admin')
        cls.team = cls.Sub._jotform_vendor_promos_team() \
            or cls.env['crm.team'].create({'name': 'Vendor Promos'})
        cls.team.user_id = cls.leader

    def _run(self, pages, dry_run=None):
        """Run the sync with the JotForm HTTP call stubbed. `pages` maps
        form_id -> list of submissions, or an Exception to raise."""
        calls = []

        def fake_get(_self, path, params, api_key):
            form_id = path.split('/')[2]
            calls.append((form_id, params))
            res = pages.get(form_id, [])
            if isinstance(res, Exception):
                raise res
            since = json.loads(params['filter'])['created_at:gt']
            return [s for s in res if s['created_at'] > since][params['offset']:
                                                                 params['offset'] + params['limit']]

        with patch.object(type(self.Sub), '_jotform_get', fake_get):
            summary = self.Sub._jotform_sync(dry_run=dry_run)
        return summary, calls

    def _imported(self, ext_id):
        return self.Sub.search([('source', '=', 'jotform'), ('external_id', '=', ext_id)])

    def test_mapping_is_lossless_and_structured(self):
        summary, _ = self._run({'F1': [_sub('9001', '2026-09-23 12:00:00')]})
        self.assertEqual(summary['created'], 1, summary)
        rec = self._imported('9001')
        self.assertEqual(rec.vendor_name, 'Test Brand')
        self.assertEqual(rec.vendor_contact, 'Pat Vendor')
        self.assertEqual(rec.vendor_email, 'pat@vendor.test')
        self.assertEqual(rec.deal_frequency, 'Weekly')
        self.assertEqual(rec.excluded_skus, 'SKU-1')
        self.assertEqual(rec.promo_units_qty, 12)
        self.assertEqual(rec.external_form_id, 'F1')
        self.assertEqual(rec.external_created_at, '2026-09-23 12:00:00')
        self.assertEqual(rec.market_id, self.region)
        self.assertIn(self.store, rec.store_ids)
        self.assertEqual(str(rec.preferred_start_date), '2026-10-01')
        self.assertEqual(str(rec.preferred_end_date), '2026-10-31')
        # create() promotes the dates into a structured plot window.
        self.assertEqual(len(rec.window_ids), 1)
        # create()'s prose parser fills the unambiguous offer.
        self.assertEqual(rec.discount_type, 'percent')
        self.assertEqual(rec.discount_value, 30.0)
        # Verbatim payload keeps questions that have no column.
        payload = json.loads(rec.jotform_payload)
        self.assertIn('unmapped but kept', [f['text'] for f in payload['fields']])

    def test_rerun_is_idempotent(self):
        pages = {'F1': [_sub('9002', '2026-09-23 12:00:00')]}
        self._run(pages)
        summary, _ = self._run(pages)
        self.assertEqual(summary['created'], 0, summary)
        self.assertEqual(summary['skipped_existing'], 1, summary)
        self.assertEqual(len(self._imported('9002')), 1)

    def test_mode_off_does_nothing(self):
        self.env['ir.config_parameter'].sudo().set_param(js.PARAM_MODE, 'off')
        summary, calls = self._run({'F1': [_sub('9003', '2026-09-23 12:00:00')]})
        self.assertEqual(summary['status'], 'disabled')
        self.assertFalse(calls)
        self.assertFalse(self._imported('9003'))

    def test_dry_run_writes_nothing(self):
        summary, _ = self._run({'F1': [_sub('9004', '2026-09-23 12:00:00')]}, dry_run=True)
        self.assertEqual(summary['mode'], 'dry')
        self.assertEqual(summary['would_create'], 1)
        self.assertFalse(self._imported('9004'))

    def test_failing_form_does_not_block_others(self):
        summary, _ = self._run({
            'F1': RuntimeError('boom'),
            'F2': [_sub('9005', '2026-09-23 12:00:00')],
        })
        self.assertEqual(summary['status'], 'partial')
        self.assertEqual(summary['forms']['ZT']['errors'], 1)
        self.assertEqual(summary['forms']['ZZ']['created'], 1)
        self.assertTrue(self._imported('9005'))

    def test_window_starts_at_newest_import_minus_lookback(self):
        self._run({'F1': [_sub('9006', '2026-09-23 12:00:00')]})
        start = self.Sub._jotform_window_start('F1', 24)
        self.assertEqual(start, '2026-09-22 12:00:00')

    def test_rpc_entry_point_is_gated(self):
        public = self.env.ref('base.public_user')
        with self.assertRaises(AccessError):
            self.Sub.with_user(public).action_jotform_sync(dry_run=True)

    # --- CRM lead + reviewer To-Do (JotForm now matches /vendor-deals) ---

    def _todos(self, rec):
        todo = self.env.ref('mail.mail_activity_data_todo')
        return rec.activity_ids.filtered(lambda a: a.activity_type_id == todo)

    def test_creates_linked_vendor_promos_lead_and_todo(self):
        summary, _ = self._run({'F1': [_sub('9101', '2026-09-23 12:00:00')]})
        self.assertEqual(summary['leads_created'], 1, summary)
        self.assertEqual(summary['todos_created'], 1, summary)
        rec = self._imported('9101')
        lead = rec.crm_lead_id
        self.assertTrue(lead)
        self.assertEqual(lead.type, 'opportunity')
        self.assertEqual(lead.team_id, self.team)
        self.assertEqual(lead.user_id, self.leader)
        self.assertEqual(lead.email_from, 'pat@vendor.test')
        self.assertIn('Test Brand', lead.name)
        self.assertIn('30% off all carts', lead.description)
        self.assertIn('9101', lead.description)
        # No reviewer configured -> the team leader gets the To-Do.
        self.assertEqual(self._todos(rec).user_id, self.leader)

    def test_reviewer_param_overrides_team_leader(self):
        reviewer = self.env.ref('base.user_root')
        self.env['ir.config_parameter'].sudo().set_param(js.PARAM_REVIEWER_USER, str(reviewer.id))
        self._run({'F1': [_sub('9102', '2026-09-23 12:00:00')]})
        self.assertEqual(self._todos(self._imported('9102')).user_id, reviewer)

    def test_lead_can_be_switched_off(self):
        self.env['ir.config_parameter'].sudo().set_param(js.PARAM_CRM_LEAD, '0')
        summary, _ = self._run({'F1': [_sub('9103', '2026-09-23 12:00:00')]})
        self.assertEqual(summary['leads_created'], 0)
        self.assertFalse(self._imported('9103').crm_lead_id)

    def test_lead_failure_keeps_submission(self):
        def boom(_self, team, form_code):
            raise ValueError('crm down')

        with patch.object(type(self.Sub), '_jotform_create_crm_lead', boom):
            summary, _ = self._run({'F1': [_sub('9104', '2026-09-23 12:00:00')]})
        self.assertEqual(summary['created'], 1, summary)
        self.assertEqual(summary['errors'], 1, summary)
        self.assertEqual(summary['status'], 'partial')
        rec = self._imported('9104')
        self.assertTrue(rec)
        self.assertFalse(rec.crm_lead_id)
        # The To-Do still lands even though the lead did not.
        self.assertTrue(self._todos(rec))
