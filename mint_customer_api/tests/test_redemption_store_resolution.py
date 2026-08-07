# -*- coding: utf-8 -*-
"""A redemption with no store in the request falls back to the home store's state.

mint.discount.create_redemption treats a missing store as "valid at any store",
so a redeem that resolves no store silently escapes per-state separation — an
AZ-issued reward becomes redeemable in FL. The frontend only sends a store when
the shopper has one selected, so the home store (x_home_store_id) is the last
line of defence before a reward goes unlocked.

_resolve_redemption_store owns that order: store_id -> store_slug -> home store.
It must also refuse a home store that cannot express a state (not a dispensary,
or no region), since locking to such a company is not per-state separation.
"""
from unittest.mock import patch

from odoo.tests.common import TransactionCase
from odoo.addons.mint_customer_api.controllers import customer as customer_mod


class _FakeRequest:
    """Minimal stand-in for odoo.http.request — the helper only needs .env."""

    def __init__(self, env):
        self.env = env


class TestRedemptionStoreResolution(TransactionCase):

    def setUp(self):
        super().setUp()
        self.ctrl = customer_mod.MintCustomerProfile()
        Company = self.env['res.company']
        Region = self.env['mint.region']

        self.region_az = Region.create({'name': 'ZZ Test Arizona', 'code': 'ZZAZ'})
        self.store_a = Company.create({
            'name': 'ZZ Test Store A',
            'is_dispensary': True,
            'region_id': self.region_az.id,
            'x_slug': 'zz-test-store-a',
        })
        self.home_store = Company.create({
            'name': 'ZZ Test Home Store',
            'is_dispensary': True,
            'region_id': self.region_az.id,
            'x_slug': 'zz-test-home-store',
        })
        self.partner = self.env['res.partner'].create({'name': 'ZZ Redemption Tester'})

    def _resolve(self, data):
        with patch.object(customer_mod, 'request', _FakeRequest(self.env)):
            return self.ctrl._resolve_redemption_store(self.partner, data)

    def _set_home_store(self, company):
        """Set x_home_store_id, or skip — it lives in mint_dutchie_sync, which is
        NOT a dependency of this module, so the field may not exist here."""
        if 'x_home_store_id' not in self.env['res.partner']._fields:
            self.skipTest('x_home_store_id absent (mint_dutchie_sync not installed)')
        self.partner.write({'x_home_store_id': company.id})

    # ── explicit store wins ──────────────────────────────────────────────

    def test_store_id_wins(self):
        self.assertEqual(self._resolve({'store_id': self.store_a.id}), self.store_a)

    def test_store_id_wins_over_home_store(self):
        self._set_home_store(self.home_store)
        self.assertEqual(self._resolve({'store_id': self.store_a.id}), self.store_a)

    def test_store_slug_used_when_no_id(self):
        self.assertEqual(self._resolve({'store_slug': 'zz-test-store-a'}), self.store_a)

    # ── the fallback ─────────────────────────────────────────────────────

    def test_falls_back_to_home_store(self):
        self._set_home_store(self.home_store)
        self.assertEqual(self._resolve({}), self.home_store)

    def test_falls_back_when_slug_unknown(self):
        """An unmatched slug must not swallow the fallback — otherwise a stale
        slug in localStorage would silently produce an unlocked reward."""
        self._set_home_store(self.home_store)
        self.assertEqual(self._resolve({'store_slug': 'no-such-store'}), self.home_store)

    def test_falls_back_when_store_id_does_not_exist(self):
        self._set_home_store(self.home_store)
        missing = self.env['res.company'].search([], order='id desc', limit=1).id + 10000
        self.assertEqual(self._resolve({'store_id': missing}), self.home_store)

    # ── home store that cannot express a state is refused ────────────────

    def test_home_store_without_region_is_refused(self):
        no_region = self.env['res.company'].create({
            'name': 'ZZ Test No Region', 'is_dispensary': True,
        })
        self._set_home_store(no_region)
        self.assertFalse(self._resolve({}))

    def test_non_dispensary_home_store_is_refused(self):
        back_office = self.env['res.company'].create({
            'name': 'ZZ Test Back Office',
            'is_dispensary': False,
            'region_id': self.region_az.id,
        })
        self._set_home_store(back_office)
        self.assertFalse(self._resolve({}))

    # ── nothing usable ───────────────────────────────────────────────────

    def test_no_store_and_no_home_store_returns_empty(self):
        self.assertFalse(self._resolve({}))

    def test_non_integer_store_id_does_not_raise(self):
        """This used to be int('abc') inside the savepoint — an uncaught
        ValueError that surfaced to the customer as a 500 'Redemption failed'
        after their points had already been deducted in the same transaction."""
        self.assertFalse(self._resolve({'store_id': 'abc'}))
