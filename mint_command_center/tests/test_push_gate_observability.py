"""Push-gate observability — a blocked push must leave a trace (Odoo #109589).

Five of six markets cannot push redemption coupons, and until phase 1 that
produced NO audit row: _push_discounts_to_dutchie returned before Log.create at
the mode gate, the market gate and the store gate, and an empty target-store
intersection simply never entered the loop. A blocked market was
indistinguishable from one that was never asked.

That mattered because the push fails soft on purpose — the points are already
deducted when it runs — so the only trace of a customer receiving a dead code
was a log line in a container that rotates.

Every case here returns BEFORE any HTTP call, so the suite makes no network
requests: the gates under test all fire ahead of the POST, and the sweeper is
only exercised on its report-only branch.

Covers AC06 (blocked push logs), AC07 (sweeper split), AC08 (per-market mode),
AC10 (re-blocking on gate flip).
"""
from odoo import fields
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestPushGateObservability(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Log = cls.env['mint.dutchie.discount.push.log']
        cls.Day = cls.env['mint.ptl.day']
        cls.Region = cls.env['mint.region']
        cls.Company = cls.env['res.company']
        cls.Discount = cls.env['mint.discount']

        # Market with the gate OFF — the live IL/MO/NV/FL/MI shape.
        cls.market_off = cls.Region.create({
            'name': 'PGO Closedland', 'code': 'PC', 'slug': 'pgo-closedland',
            'dutchie_discount_push_enabled': False,
        })
        cls.store_off = cls.Company.create({
            'name': 'PGO Closed Store', 'is_dispensary': True,
            'dutchie_store_id': 'pgo-closed-uuid', 'region_id': cls.market_off.id,
            'dutchie_discount_push_enabled': False,
        })
        cls.day_off = cls.Day.create({'market_id': cls.market_off.id,
                                      'date': fields.Date.today()})

        # Market with the gate ON and one push-enabled store — the AZ shape.
        cls.market_on = cls.Region.create({
            'name': 'PGO Openland', 'code': 'PO', 'slug': 'pgo-openland',
            'dutchie_discount_push_enabled': True,
        })
        cls.store_on = cls.Company.create({
            'name': 'PGO Open Store', 'is_dispensary': True,
            'dutchie_store_id': 'pgo-open-uuid', 'region_id': cls.market_on.id,
            'dutchie_discount_push_enabled': True,
        })
        cls.store_on_disabled = cls.Company.create({
            'name': 'PGO Open Store (not pushing)', 'is_dispensary': True,
            'dutchie_store_id': 'pgo-open-uuid-2', 'region_id': cls.market_on.id,
            'dutchie_discount_push_enabled': False,
        })
        cls.day_on = cls.Day.create({'market_id': cls.market_on.id,
                                     'date': fields.Date.today()})

        cls.product = cls.env['product.template'].create({
            'name': 'PGO Test Product', 'type': 'consu',
            'dutchie_product_id': '99000001',
        })

    def _redemption(self, stores, code='PGOTEST01'):
        return self.Discount.create({
            'name': 'PGO redemption %s' % code,
            'discount_type': 'loyalty_redemption',
            'redemption_code': code, 'dutchie_discount_code': code,
            'product_ids': [(6, 0, self.product.ids)],
            'redemption_product_id': self.product.id,
            'redemption_status': 'pending',
            'maximum_usage_count': 1,
            'store_ids': [(6, 0, stores.ids)],
        })

    def _rows(self, discount):
        return self.Log.search([('discount_id', '=', discount.id)])

    # ---- AC06: a blocked push must write a row -----------------------------

    def test_P0_1_mode_off_writes_a_row(self):
        """mode='off' used to `return  # ZERO behavior change` with no trace."""
        d = self._redemption(self.store_on, 'PGOMODEOFF')
        self.env['ir.config_parameter'].sudo().set_param(
            'mint.dutchie_discount_push.mode', 'off')
        self.day_on._push_discounts_to_dutchie(d.ids)
        rows = self._rows(d)
        self.assertEqual(len(rows), 1, 'expected exactly one blocked-push row')
        self.assertFalse(rows.success)
        self.assertIn('push_mode_off', rows.error_message)

    def test_P0_2_market_gate_off_writes_a_row_naming_the_market(self):
        """The live IL/MO/NV/FL/MI case — the one that burned nobody only
        because no non-AZ redemption had happened yet."""
        d = self._redemption(self.store_off, 'PGOMKTOFF')
        self.env['ir.config_parameter'].sudo().set_param(
            'mint.dutchie_discount_push.mode', 'live')
        self.day_off._push_discounts_to_dutchie(d.ids)
        rows = self._rows(d)
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows.success)
        self.assertIn('market_push_disabled:PC', rows.error_message,
                      'the reason must name the market so it can be filtered')

    def test_P0_3_no_enabled_store_writes_a_row(self):
        market = self.Region.create({
            'name': 'PGO Halfland', 'code': 'PH', 'slug': 'pgo-halfland',
            'dutchie_discount_push_enabled': True,
        })
        store = self.Company.create({
            'name': 'PGO Half Store', 'is_dispensary': True,
            'dutchie_store_id': 'pgo-half-uuid', 'region_id': market.id,
            'dutchie_discount_push_enabled': False,
        })
        day = self.Day.create({'market_id': market.id, 'date': fields.Date.today()})
        d = self._redemption(store, 'PGONOSTORE')
        self.env['ir.config_parameter'].sudo().set_param(
            'mint.dutchie_discount_push.mode', 'live')
        day._push_discounts_to_dutchie(d.ids)
        rows = self._rows(d)
        self.assertEqual(len(rows), 1)
        self.assertIn('no_push_enabled_store_in_market:PH', rows.error_message)

    def test_P0_4_empty_target_store_overlap_writes_a_row(self):
        """The subtlest hole: the market and a store are both enabled, but the
        discount is scoped to a DIFFERENT store, so the intersection is empty
        and the for-loop simply never ran."""
        d = self._redemption(self.store_on_disabled, 'PGONOOVERLAP')
        self.env['ir.config_parameter'].sudo().set_param(
            'mint.dutchie_discount_push.mode', 'live')
        self.day_on._push_discounts_to_dutchie(d.ids)
        rows = self._rows(d)
        self.assertEqual(len(rows), 1, 'an empty store intersection must be logged')
        self.assertIn('no_target_store_overlap', rows.error_message)

    # ---- AC08: per-market mode override ------------------------------------

    def test_P0_5_market_mode_overrides_the_global(self):
        """Without this, dry-running a new market means flipping the global
        flag — which is 'live' — and suppressing Arizona for the duration."""
        self.env['ir.config_parameter'].sudo().set_param(
            'mint.dutchie_discount_push.mode', 'live')
        self.market_on.dutchie_push_mode = 'dry-run'
        self.assertEqual(self.day_on._get_dutchie_push_mode(), 'dry-run')
        self.market_on.dutchie_push_mode = False
        self.assertEqual(self.day_on._get_dutchie_push_mode(), 'live',
                         'empty must inherit the global setting')

    def test_P0_6_empty_recordset_falls_back_to_global(self):
        """Regression guard. _get_dutchie_push_mode is called on an EMPTY
        mint.ptl.day at five of its seven call sites (the welcome-coupon sync
        does exactly that). Guarding on bool(self) instead of len(self) == 1
        would raise 'Expected singleton' on a multi-record set."""
        self.env['ir.config_parameter'].sudo().set_param(
            'mint.dutchie_discount_push.mode', 'live')
        self.assertEqual(self.Day._get_dutchie_push_mode(), 'live')
        pair = self.day_on | self.day_off
        self.assertEqual(pair._get_dutchie_push_mode(), 'live',
                         'a multi-record set must not raise')

    # ---- AC07: the sweeper -------------------------------------------------

    def _backdate(self, record, minutes=30):
        """The sweep has a 15-minute floor; create_date is not writable."""
        self.env.cr.execute(
            "UPDATE mint_discount SET create_date = NOW() - INTERVAL '%s minutes' "
            "WHERE id = %s", (minutes, record.id))
        record.invalidate_recordset(['create_date'])

    def test_P0_7_sweeper_reports_a_blocked_redemption_without_pushing(self):
        """A closed market is a rollout state, not an error: re-pushing every
        hour would fail every hour and bury the real failures."""
        d = self._redemption(self.store_off, 'PGOSWEEPBLOCK')
        self._backdate(d)
        before = len(self._rows(d))
        self.Discount._cron_sweep_unpushed_redemptions()
        self.assertEqual(d.redemption_status, 'pending', 'must not be mutated')
        self.assertEqual(len(self._rows(d)), before,
                         'a still-blocked market must be reported, not retried into failure')

    def test_P0_8_sweeper_skips_redemptions_already_live_in_dutchie(self):
        """Selection is 'no successful live push-log row' — NOT
        dutchie_discount_id, which lives on the log per (discount, store) by
        design and is never set on mint.discount."""
        d = self._redemption(self.store_on, 'PGOSWEEPDONE')
        self._backdate(d)
        self.Log.create({
            'discount_id': d.id, 'company_id': self.store_on.id,
            'mode': 'live', 'success': True, 'dutchie_discount_id': 987654,
        })
        before = len(self._rows(d))
        self.Discount._cron_sweep_unpushed_redemptions()
        self.assertEqual(len(self._rows(d)), before,
                         'an already-pushed redemption must not be touched')

    # ---- AC10: flipping the gate back re-blocks ----------------------------

    def test_P0_9_reblocking_is_immediate_on_gate_flip(self):
        """Rollback for a market is a boolean flip with no deploy — so the
        predicate must react to it at once."""
        self.assertIsNone(
            self.Discount.redemption_push_blocked_reason(self.store_on))
        self.market_on.dutchie_discount_push_enabled = False
        reason = self.Discount.redemption_push_blocked_reason(self.store_on)
        self.assertTrue(reason and reason.startswith('market_push_disabled'),
                        'flipping the gate back must re-block immediately, got %r' % reason)
        self.market_on.dutchie_discount_push_enabled = True
        self.assertIsNone(
            self.Discount.redemption_push_blocked_reason(self.store_on))
