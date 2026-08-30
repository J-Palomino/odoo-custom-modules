"""
An unscoped PTL discount must reach its OWN market, and nowhere else.

On 2026-08-25 restoring the PTL lifecycle cron pushed Arizona deals into all
40 stores. Two things combined:

  - 246 of 251 published PTL discounts carry no store_ids, and
    _push_discounts_to_redis read that as "every store in the map";
  - _get_store_uuid_map only narrowed by market when `self.market_id` was set,
    and the crons call it through an EMPTY mint.ptl.day recordset, where that
    is silently falsy — so the map was every store in EVERY market.

All 17 Florida stores (a compliance-isolation market) plus MI, IL, MO and NV
served Canamo / The Pharm / WTF Extracts at Arizona prices. 5,859 stray rows
had to be deleted afterwards.

The market now comes from the discount's own PTL deal, and a discount with no
resolvable market is skipped rather than broadcast.
"""
from unittest.mock import patch

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.mint_command_center.models import ptl_day as ptl_day_module


@tagged("post_install", "-at_install")
class TestPtlMarketScoping(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Day = cls.env['mint.ptl.day']
        Region = cls.env['mint.region']
        cls.az = Region.create({'name': 'SCOPING Arizona'})
        cls.fl = Region.create({'name': 'SCOPING Florida'})

        # Two stores per market. Ids are what the payload grouping keys on.
        cls.az_stores = {101: 'az-uuid-1', 102: 'az-uuid-2'}
        cls.fl_stores = {201: 'fl-uuid-1', 202: 'fl-uuid-2'}
        cls.all_stores = {**cls.az_stores, **cls.fl_stores}

        cls.az_deal = cls.env['mint.ptl.deal'].create(
            {'name': 'SCOPING AZ deal', 'market_id': cls.az.id})
        cls.no_market_deal = cls.env['mint.ptl.deal'].create(
            {'name': 'SCOPING marketless deal'})

    def _discount(self, name, deal=None):
        vals = {
            'name': name, 'source': 'ptl',
            'discount_type': 'percent', 'discount_amount': 0.3,
        }
        if deal is not None:
            vals['ptl_deal_id'] = deal.id
        return self.env['mint.discount'].create(vals)

    def _push(self, discounts, caller=None):
        """Push and return {uuid: [discount_name, ...]} that would be sent."""
        sent = {}
        Day = type(self.Day)
        all_stores, az_stores, fl_stores = self.all_stores, self.az_stores, self.fl_stores
        az_id, fl_id = self.az.id, self.fl.id

        def fake_map(_self, market=None):
            if market is None:
                return all_stores
            if market.id == az_id:
                return az_stores
            if market.id == fl_id:
                return fl_stores
            return {}

        def fake_payload(_self, discount, uuid):
            sent.setdefault(uuid, []).append(discount.name)
            return {'location_id': uuid, 'name': discount.name}

        class InertThread:
            def __init__(self, *a, **kw):
                pass

            def start(self):
                pass

        with patch.object(Day, '_get_store_uuid_map', fake_map), \
                patch.object(Day, '_discount_to_webhook_payload', fake_payload), \
                patch.object(ptl_day_module.threading, 'Thread', InertThread):
            (caller if caller is not None else self.Day)._push_discounts_to_redis(discounts.ids)
        return sent

    def test_unscoped_discount_reaches_only_its_own_market(self):
        """The regression: an Arizona deal must not land in Florida."""
        d = self._discount('SCOPING az promo', self.az_deal)
        sent = self._push(d)

        self.assertEqual(
            sorted(sent), sorted(self.az_stores.values()),
            'an unscoped Arizona discount must reach Arizona stores only')
        for uuid in self.fl_stores.values():
            self.assertNotIn(uuid, sent)

    def test_discount_with_no_resolvable_market_is_skipped(self):
        """Skipping costs one missing card; broadcasting is the wrong price in
        five states."""
        d = self._discount('SCOPING marketless promo', self.no_market_deal)
        sent = self._push(d)
        self.assertEqual(sent, {}, 'a marketless unscoped discount must not be pushed anywhere')

    def test_discount_with_no_deal_at_all_is_skipped(self):
        d = self._discount('SCOPING dealless promo')
        sent = self._push(d)
        self.assertEqual(sent, {})

    def test_explicit_store_ids_still_win(self):
        """A discount that names its stores is authoritative — the market
        lookup must not override or widen it."""
        company = self.env['res.company'].create({'name': 'SCOPING Store 101'})
        d = self._discount('SCOPING explicit promo', self.az_deal)
        d.store_ids = [(6, 0, [company.id])]

        all_stores = {company.id: 'explicit-uuid'}
        Day = type(self.Day)
        sent = {}

        def fake_map(_self, market=None):
            return all_stores if market is None else {}

        def fake_payload(_self, discount, uuid):
            sent.setdefault(uuid, []).append(discount.name)
            return {}

        class InertThread:
            def __init__(self, *a, **kw):
                pass

            def start(self):
                pass

        with patch.object(Day, '_get_store_uuid_map', fake_map), \
                patch.object(Day, '_discount_to_webhook_payload', fake_payload), \
                patch.object(ptl_day_module.threading, 'Thread', InertThread):
            self.Day._push_discounts_to_redis(d.ids)

        self.assertEqual(list(sent), ['explicit-uuid'])

    def test_falls_back_to_the_publishing_day_market(self):
        """action_publish runs on a real day that knows its market, so a
        discount whose deal lost its market still publishes correctly there."""
        day = self.env['mint.ptl.day'].create({
            'date': '2026-09-01', 'market_id': self.fl.id,
        })
        d = self._discount('SCOPING fallback promo', self.no_market_deal)
        sent = self._push(d, caller=day)

        self.assertEqual(
            sorted(sent), sorted(self.fl_stores.values()),
            "should fall back to the day's own market rather than skipping")

    def test_get_store_uuid_map_ignores_self_market(self):
        """The map must key off its argument, never off self — reading self is
        what made an empty recordset mean 'every market'."""
        day = self.env['mint.ptl.day'].create({
            'date': '2026-09-02', 'market_id': self.fl.id,
        })
        empty = self.env['res.company']
        captured = []

        def spy(_self, domain, **kw):
            captured.append(domain)
            return empty

        with patch.object(type(empty), 'search', spy):
            day.sudo()._get_store_uuid_map()               # no market argument
            day.sudo()._get_store_uuid_map(self.az)        # explicit market

        def region_leaves(domain):
            return [l for l in domain
                    if isinstance(l, (list, tuple)) and l[0] == 'region_id']

        self.assertEqual(
            region_leaves(captured[0]), [],
            'calling with no market must NOT silently narrow by self.market_id')
        self.assertEqual(
            region_leaves(captured[1]), [('region_id', '=', self.az.id)],
            'an explicit market must narrow to that market')
