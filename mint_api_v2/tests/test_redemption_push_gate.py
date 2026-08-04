"""The redemption push gate, asked BEFORE points are spent.

The Dutchie push fails soft on purpose: by the time it runs the points are
already deducted and the mint.discount already exists in the caller's
transaction, so raising there would cost the customer their points AND their
coupon. The price of that choice is that a blocked push is invisible — the
customer is debited and handed a code no register accepts, with no push-log row
(the market gate returns before one is written) and nothing to retry it.

redemption_push_blocked_reason() lets the caller ask first, so a redemption that
cannot reach Dutchie is refused instead of sold.

Live state when this was written: 1 of 43 stores had dutchie_discount_push_enabled
and only Arizona's market gate was on, while reward catalogs were live in IL, MO
and NV — so the first redemption in those states would have burned a customer.
"""
from odoo import fields
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestRedemptionPushGate(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Discount = cls.env['mint.discount']
        cls.Region = cls.env['mint.region']
        cls.Company = cls.env['res.company']

        # A market that can push: gate on, a ptl.day carrying it, an enabled store.
        cls.market_ok = cls.Region.create({
            'name': 'RPG Openland', 'code': 'RO', 'slug': 'rpg-openland',
            'dutchie_discount_push_enabled': True,
        })
        cls.store_ok = cls.Company.create({
            'name': 'RPG Open Store', 'is_dispensary': True,
            'dutchie_store_id': 'rpg-open-uuid', 'region_id': cls.market_ok.id,
            'dutchie_discount_push_enabled': True,
        })
        cls.env['mint.ptl.day'].create({'market_id': cls.market_ok.id,
                                       'date': fields.Date.today()})

        # A market with the gate OFF — the live IL/MO/NV/FL/MI shape.
        cls.market_off = cls.Region.create({
            'name': 'RPG Closedland', 'code': 'RC', 'slug': 'rpg-closedland',
            'dutchie_discount_push_enabled': False,
        })
        cls.store_off = cls.Company.create({
            'name': 'RPG Closed Store', 'is_dispensary': True,
            'dutchie_store_id': 'rpg-closed-uuid', 'region_id': cls.market_off.id,
            'dutchie_discount_push_enabled': False,
        })
        cls.env['mint.ptl.day'].create({'market_id': cls.market_off.id,
                                       'date': fields.Date.today()})

    def test_open_market_is_not_blocked(self):
        """Gate on + ptl.day + an enabled store => the push would go through."""
        self.assertIsNone(
            self.Discount.redemption_push_blocked_reason(self.store_ok))

    def test_market_gate_off_is_blocked(self):
        """The live IL/MO/NV case: catalog visible, push impossible."""
        reason = self.Discount.redemption_push_blocked_reason(self.store_off)
        self.assertTrue(reason and reason.startswith('market_push_disabled'),
                        'expected market_push_disabled, got %r' % reason)

    def test_no_store_is_blocked(self):
        """A redemption with no store cannot be region-locked OR pushed."""
        self.assertEqual(
            self.Discount.redemption_push_blocked_reason(False), 'no_store')
        self.assertEqual(
            self.Discount.redemption_push_blocked_reason(None), 'no_store')

    def test_store_without_region_is_blocked(self):
        orphan = self.Company.create({
            'name': 'RPG Orphan', 'is_dispensary': True,
            'dutchie_store_id': 'rpg-orphan-uuid',
        })
        self.assertEqual(
            self.Discount.redemption_push_blocked_reason(orphan),
            'store_has_no_region')

    def test_market_with_no_ptl_day_is_blocked(self):
        """The pusher hangs off a mint.ptl.day carrying the market; without one
        there is no carrier to push through."""
        bare = self.Region.create({
            'name': 'RPG Bareland', 'code': 'RB', 'slug': 'rpg-bareland',
            'dutchie_discount_push_enabled': True,
        })
        store = self.Company.create({
            'name': 'RPG Bare Store', 'is_dispensary': True,
            'dutchie_store_id': 'rpg-bare-uuid', 'region_id': bare.id,
            'dutchie_discount_push_enabled': True,
        })
        reason = self.Discount.redemption_push_blocked_reason(store)
        self.assertTrue(reason and reason.startswith('no_ptl_day_for_market'),
                        'expected no_ptl_day_for_market, got %r' % reason)

    def test_enabled_market_but_no_enabled_store_is_blocked(self):
        """The pusher intersects the discount's stores with push-enabled ones.
        An empty intersection pushes nothing and writes no log row, so the
        market gate alone is not sufficient."""
        market = self.Region.create({
            'name': 'RPG Halfland', 'code': 'RH', 'slug': 'rpg-halfland',
            'dutchie_discount_push_enabled': True,
        })
        store = self.Company.create({
            'name': 'RPG Half Store', 'is_dispensary': True,
            'dutchie_store_id': 'rpg-half-uuid', 'region_id': market.id,
            'dutchie_discount_push_enabled': False,
        })
        self.env['mint.ptl.day'].create({'market_id': market.id,
                                         'date': fields.Date.today()})
        reason = self.Discount.redemption_push_blocked_reason(store)
        self.assertTrue(
            reason and reason.startswith('no_push_enabled_store_in_market'),
            'expected no_push_enabled_store_in_market, got %r' % reason)

    def test_gate_matches_what_the_pusher_actually_does(self):
        """The predicate is only useful if it agrees with the push path.

        Asserting the open market is unblocked AND the closed one is blocked
        pins both directions: a predicate that always returned None would sell
        dead codes, and one that always returned a reason would block Arizona,
        which demonstrably works today.
        """
        self.assertIsNone(self.Discount.redemption_push_blocked_reason(self.store_ok))
        self.assertIsNotNone(self.Discount.redemption_push_blocked_reason(self.store_off))
