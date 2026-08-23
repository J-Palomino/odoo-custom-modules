# -*- coding: utf-8 -*-
"""Phase 6 partner merge tests (issue #278).

Covers the decided cron policy (one strong match = merge, ambiguity = flag),
the UNIQUE-violation dance around the stock wizard's _update_values, and the
guards (staff, conflicting keys, wizard refusals).
"""
from odoo import fields
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestPartnerMerge(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Partner = cls.env['res.partner']
        cls.Log = cls.env['mint.partner.merge.log']
        # Dry-run OFF for tests unless a test opts in.
        cls.env['ir.config_parameter'].sudo().set_param(
            'mint_pos_bridge.partner_merge_dry_run', '0')

    def _stub(self, name='Walk-in #1', **vals):
        base = {
            'name': name,
            'customer_rank': 1,
            'x_is_stub': True,
            'x_partner_origin': 'dutchie_walkin',
            'x_first_seen_at': fields.Datetime.now(),
        }
        base.update(vals)
        return self.Partner.create(base)

    def _full(self, name='Jane Doe', **vals):
        base = {'name': name, 'customer_rank': 1}
        base.update(vals)
        return self.Partner.create(base)

    def test_auto_merge_single_strong_match(self):
        primary = self._full(x_dutchie_loyalty_id='L-100', phone='(480) 555-0100')
        stub = self._stub(x_dutchie_loyalty_id='L-100')
        order = self.env['mint.pos.order'].create({
            'partner_id': stub.id,
            'company_id': self.env.company.id,
        })

        result = self.Partner._cron_dedupe_stubs(batch_limit=50, dry_run=False)

        self.assertEqual(result['merged'], 1)
        self.assertFalse(stub.exists(), 'source stub must be deleted')
        self.assertEqual(order.partner_id, primary, 'order FK must follow the merge')
        self.assertTrue(primary.x_last_merged_at)
        log = self.Log.search([('dst_partner_id', '=', primary.id)])
        self.assertEqual(len(log), 1)
        self.assertEqual(log.mode, 'auto')

    def test_unique_key_survives_merge(self):
        """Source holds the UNIQUE x_dutchie_customer_id, destination empty.
        Without the blank-first dance the wizard's _update_values trips the
        constraint while both rows still exist."""
        primary = self._full(phone='4805550101')
        stub = self._stub(x_dutchie_customer_id='D-424242',
                          x_dutchie_loyalty_id='L-200')
        primary.x_dutchie_loyalty_id = 'L-200'

        status, detail = stub._merge_into(primary, mode='auto',
                                          matched_key='loyalty_id')

        self.assertEqual(status, 'merged', detail)
        self.assertEqual(primary.x_dutchie_customer_id, 'D-424242',
                         'strong key union must land on the survivor')

    def test_conflicting_strong_keys_never_merge(self):
        primary = self._full(x_dutchie_customer_id='D-1', x_dutchie_loyalty_id='L-300')
        stub = self._stub(x_dutchie_customer_id='D-2', x_dutchie_loyalty_id='L-300')

        status, detail = stub._merge_into(primary)

        self.assertEqual(status, 'conflict')
        self.assertTrue(stub.exists())
        self.assertTrue(stub.x_merge_needs_review)
        self.assertTrue(primary.x_merge_needs_review)

    def test_two_strong_candidates_flag_only(self):
        self._full(name='A', x_dutchie_loyalty_id='L-400')
        self._full(name='B', x_dutchie_loyalty_id='L-400')
        stub = self._stub(x_dutchie_loyalty_id='L-400')

        self.Partner._cron_dedupe_stubs(batch_limit=50, dry_run=False)

        self.assertTrue(stub.exists())
        self.assertTrue(stub.x_merge_needs_review)

    def test_weak_only_candidates_flag_only(self):
        self._full(phone='(480) 555-0777')
        stub = self._stub(phone='480-555-0777')

        self.Partner._cron_dedupe_stubs(batch_limit=50, dry_run=False)

        self.assertTrue(stub.exists(), 'weak-key match must never auto-merge')
        self.assertTrue(stub.x_merge_needs_review)

    def test_nd_identity_key_is_weak(self):
        # x_dutchie_identity_key ships with mint_dutchie_sync >= 19.0.1.4;
        # older installs (staging) do not have it and skip this tier.
        if 'x_dutchie_identity_key' not in self.Partner._fields:
            self.skipTest('mint_dutchie_sync too old: no x_dutchie_identity_key')
        self._full(x_dutchie_identity_key='nd:JANE DOE|01/01/1990')
        stub = self._stub(x_dutchie_identity_key='nd:JANE DOE|01/01/1990')

        strong, weak = stub._find_merge_candidates()

        self.assertFalse(strong)
        self.assertEqual(len(weak), 1)

    def test_dl_identity_key_is_strong(self):
        if 'x_dutchie_identity_key' not in self.Partner._fields:
            self.skipTest('mint_dutchie_sync too old: no x_dutchie_identity_key')
        full = self._full(x_dutchie_identity_key='dl:D05227838')
        stub = self._stub(x_dutchie_identity_key='dl:D05227838')

        strong, _weak = stub._find_merge_candidates()

        self.assertEqual(strong, full)

    def test_merge_works_without_identity_key_field(self):
        """The module must load and merge on installs whose mint_dutchie_sync
        predates x_dutchie_identity_key (the staging case that caught this)."""
        fields_present = self.Partner._merge_strong_fields()
        self.assertIn('x_dutchie_customer_id', fields_present)
        for name in fields_present:
            self.assertIn(name, self.Partner._fields)

    def test_staff_partner_never_merged(self):
        staff = self._full(x_dutchie_loyalty_id='L-500', employee=True)
        stub = self._stub(x_dutchie_loyalty_id='L-500')

        status, detail = stub._merge_into(staff)

        self.assertEqual(status, 'skipped')
        self.assertEqual(detail, 'staff partner')
        self.assertTrue(stub.exists())

    def test_dry_run_mutates_nothing(self):
        primary = self._full(x_dutchie_loyalty_id='L-600')
        stub = self._stub(x_dutchie_loyalty_id='L-600')

        result = self.Partner._cron_dedupe_stubs(batch_limit=50, dry_run=True)

        self.assertTrue(result['dry_run'])
        self.assertTrue(stub.exists())
        self.assertFalse(stub.x_merge_needs_review)
        log = self.Log.search([('dst_partner_id', '=', primary.id),
                               ('mode', '=', 'dry_run')])
        self.assertEqual(len(log), 1, 'dry run must write its would-merge row')

    def test_wizard_refusal_flags_for_review(self):
        """Partners linked to users make the stock wizard raise - the merge
        must degrade to a review flag, not an exception."""
        primary = self._full(x_dutchie_loyalty_id='L-700')
        stub = self._stub(x_dutchie_loyalty_id='L-700')
        for i, partner in enumerate((primary, stub)):
            self.env['res.users'].create({
                'name': partner.name or 'user%d' % i,
                'login': 'merge_test_user_%d@example.com' % i,
                'partner_id': partner.id,
                # Portal users (share=True) so the staff guard does not skip
                # the pair before the wizard refusal path is reached.
                'group_ids': [(6, 0, [self.env.ref('base.group_portal').id])],
            })

        status, _detail = stub._merge_into(primary)

        self.assertEqual(status, 'failed')
        self.assertTrue(stub.exists())
        self.assertTrue(stub.x_merge_needs_review)
        self.assertTrue(primary.x_merge_needs_review)
