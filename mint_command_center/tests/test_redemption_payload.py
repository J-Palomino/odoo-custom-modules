"""Regression: the Dutchie payload for a loyalty redemption.

A redemption is "this one product, free, once, at the register". Two live
incidents produced coupons that were accepted by Dutchie and still useless:

  1. **Unscoped / valueless.** The generic PTL payload path reads the reward
     shape off the mint.discount record, and a redemption carries none — so
     Dutchie stored DiscountValue 0 with EMPTY restrictions. That is not "no
     discount", it is 100% off the ENTIRE CATALOG, and seven of them were
     published live before being withdrawn.
  2. **Platform-locked.** The same path sends
     PlatformTypeRestrictions [{PlatformTypeId: 2}] ("Online"). On an in-store
     coupon that makes it redeemable nowhere; the register answers "this
     coupon is not available on this origin platform".

The shape asserted here is not invented: it is read back from the live Dutchie
records 385328-385334, each of which returns ``verified: true`` from
``GET /api/admin/discounts/:id`` and scans at an AZ-Tempe register.

The reward values deliberately live in the payload builder and NOT on the
mint.discount record. Writing calculation_method_id onto the record makes
mint_dutchie_discount_mirror derive ``discount_type='percent'``, which drops
the coupon out of every /rewards and consume query — a regression shipped and
reverted twice (PR #263).
"""
from odoo.tests import TransactionCase, tagged

# Live-verified constants (Dutchie 385328-385334). Changing any of these means
# changing what the register does — verify against a real record first.
APPLICATION_METHOD_CODE = 3   # customer presents a code
CALC_METHOD_PERCENT = 2       # with DiscountValue 1 => 100% off
ITEM_GROUP_SINGLE_ITEM = 5
THRESHOLD_TYPE_ITEM = 1

DAYS = ('Sunday', 'Monday', 'Tuesday', 'Wednesday',
        'Thursday', 'Friday', 'Saturday')


@tagged('post_install', '-at_install')
class TestRedemptionPayload(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.product = cls.env['product.template'].create({
            'name': 'RP Test — Live Resin Cart',
            'type': 'consu',
            'dutchie_product_id': '13798789',
        })
        cls.store = cls.env['res.company'].create({
            'name': 'RP Test Store',
            'dutchie_store_id': 'rp-test-uuid',
        })
        cls.discount = cls.env['mint.discount'].create({
            'name': 'Redemption: RP Test — Live Resin Cart',
            'discount_type': 'loyalty_redemption',
            'redemption_code': 'B515238939',
            'dutchie_discount_code': 'B515238939',
            'product_ids': [(6, 0, cls.product.ids)],
            'redemption_product_id': cls.product.id,
            'maximum_usage_count': 1,
        })
        # The builder hangs off the pusher mixin; an empty mint.ptl.day is the
        # documented carrier for coupons that belong to no market or day.
        cls.carrier = cls.env['mint.ptl.day']

    def _build(self):
        return self.carrier._redemption_payload(self.discount, self.store, 0)

    def test_reward_is_100_percent_off_one_item(self):
        """DiscountValue 1 at CalculationMethodId 2 == 100% off, single item.

        A null/0 value is the incident-1 signature: Dutchie accepts it and the
        customer is charged full price.
        """
        reward = self._build()['Reward']
        self.assertEqual(reward['CalculationMethodId'], CALC_METHOD_PERCENT)
        self.assertEqual(reward['DiscountValue'], 1)
        self.assertEqual(reward['ItemGroupTypeId'], ITEM_GROUP_SINGLE_ITEM)
        self.assertTrue(reward['HasThreshold'])
        self.assertEqual(
            (reward['ThresholdMin'], reward['ThresholdMax'],
             reward['ThresholdTypeId']),
            (1, 1, THRESHOLD_TYPE_ITEM),
            'threshold must pin the reward to exactly one item')

    def test_scoped_to_the_redeemed_product_only(self):
        """The Product slot carries the Dutchie id; every other slot is empty.

        An empty Product slot does not mean "no products" to Dutchie — it means
        "all of them".
        """
        restrictions = self._build()['Reward']['Restrictions']
        self.assertEqual(restrictions['Product'],
                         {'IsExclusion': False, 'RestrictionIds': [13798789]})
        for slot, value in restrictions.items():
            if slot != 'Product':
                self.assertEqual(
                    value, {'IsExclusion': False, 'RestrictionIds': []},
                    '%s must be present and empty' % slot)

    def test_no_platform_lock(self):
        """Incident 2: any PlatformTypeRestrictions entry kills in-store use."""
        payload = self._build()
        self.assertEqual(payload['PlatformTypeRestrictions'], [])
        self.assertFalse(payload['IsAvailableOnline'])

    def test_single_use_code_coupon(self):
        payload = self._build()
        self.assertEqual(payload['ApplicationMethodId'], APPLICATION_METHOD_CODE)
        self.assertEqual(payload['DiscountCode'], 'B515238939')
        self.assertEqual(payload['MaxRedemptions'], 1)
        self.assertIsNone(payload['RedemptionLimit'])
        self.assertEqual(payload['RedemptionLimitCountingMode'], 0)

    def test_not_day_scoped(self):
        """All-None, not all-False: a redemption is valid any day it is live."""
        payload = self._build()
        for day in DAYS:
            self.assertIsNone(payload[day], '%s must be None' % day)

    def test_refuses_to_build_without_a_dutchie_product_id(self):
        """No id means no scope, and no scope means the whole catalog.

        Returning None is what makes the caller log and skip instead of POSTing
        a catalog-wide 100%-off.
        """
        self.product.dutchie_product_id = False
        self.assertIsNone(self._build())

    def test_dispatch_routes_redemptions_to_this_builder(self):
        """The type check, not the caller, decides — a redemption must never
        fall through to the PTL path that produced both incidents."""
        via_dispatch = self.carrier._deal_to_dutchie_payload(
            self.discount, self.store)
        self.assertEqual(via_dispatch['Reward']['DiscountValue'], 1)
        self.assertEqual(via_dispatch['PlatformTypeRestrictions'], [])

    def test_sibling_scope_resolves_per_store(self):
        """A widened scope pushes each store its OWN product id.

        create_redemption widens product_ids to the sibling templates of the
        redeemed product — same item at the region's other stores, each under
        that store's per-location Dutchie id. The payload for a store must
        carry the id from that store's catalog, not the slid template's: a
        foreign id matches nothing at the register.
        """
        Fields = self.env['ir.model.fields']
        if 'x_location_id' not in self.env['product.template']._fields:
            Fields.create({
                'name': 'x_location_id',
                'model': 'product.template',
                'model_id': self.env['ir.model']._get_id('product.template'),
                'ttype': 'char',
                'state': 'manual',
            })
        self.product.x_location_id = 'rp-test-uuid'
        sibling = self.env['product.template'].create({
            'name': self.product.name,
            'type': 'consu',
            'dutchie_product_id': '13800001',
            'x_location_id': 'rp-sibling-uuid',
        })
        other_store = self.env['res.company'].create({
            'name': 'RP Sibling Store',
            'dutchie_store_id': 'rp-sibling-uuid',
        })
        self.discount.product_ids = [(6, 0, (self.product + sibling).ids)]

        at_home = self.carrier._redemption_payload(self.discount, self.store, 0)
        self.assertEqual(
            at_home['Reward']['Restrictions']['Product']['RestrictionIds'],
            [13798789])
        at_sibling = self.carrier._redemption_payload(
            self.discount, other_store, 0)
        self.assertEqual(
            at_sibling['Reward']['Restrictions']['Product']['RestrictionIds'],
            [13800001])

        # A store carrying NO local template falls back to the whole group —
        # ids foreign to it match nothing, which beats pushing unscoped.
        stranger = self.env['res.company'].create({
            'name': 'RP Stranger Store',
            'dutchie_store_id': 'rp-stranger-uuid',
        })
        at_stranger = self.carrier._redemption_payload(
            self.discount, stranger, 0)
        self.assertEqual(
            at_stranger['Reward']['Restrictions']['Product']['RestrictionIds'],
            [13798789, 13800001])

    def test_record_keeps_its_loyalty_redemption_type(self):
        """PR #263 guard: the reward shape lives in the payload only.

        If a future change writes calculation_method_id onto the record,
        mint_dutchie_discount_mirror retypes it to 'percent' and the coupon
        vanishes from /rewards while still existing in Dutchie.
        """
        self._build()
        self.discount.invalidate_recordset()
        self.assertEqual(self.discount.discount_type, 'loyalty_redemption')
