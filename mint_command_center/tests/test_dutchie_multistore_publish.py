"""Regression: the deal-submission Dutchie publish path emits ONE multi-store
record per span, never one record per store.

Guards PR #179. Before it, ``_dutchie_publish_after_convert`` looped over every
LocId and POSTed a separate discount per store (N stores -> N records, each
store-wide because ``LocationRestrictions`` was empty). The fix collapses that
to ONE record per span whose ``LocationRestrictions`` array carries every target
store, owned by a non-zero LocId (the invsvc ``/api/admin/discounts`` route
rejects ``locId == 0``).

These tests stub ``_dutchie_build`` (so they don't depend on brand/category/
market fixtures or the span-partitioning internals — those are covered
elsewhere) and assert the two invariants the consolidation rests on:

  (a) every POSTed discount carries ``LocationRestrictions == loc_ids``;
  (b) the number of POSTs equals the number of SPANS, not the number of stores.
"""
import json
from unittest import mock

from odoo.tests import TransactionCase, tagged

PUBLISH_MODULE = 'odoo.addons.mint_command_center.models.dutchie_publish'

# LSP 575 = Arizona; three AZ store LocIds. owner_loc must be the first.
LSP = 575
LOC_IDS = [1568, 1570, 2272]


class _FakeResp:
    """Context-manager stand-in for urllib's HTTPResponse."""

    def __init__(self, body, status=200):
        self._body = body.encode()
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def _build_result(external_ids):
    """A fake _dutchie_build() return with one span per external id."""
    return {
        'lsp': LSP,
        'warnings': [],
        'discounts': [
            {
                'ExternalId': ext,
                'OnlineName': 'Test Deal',
                'ValidDateFrom': '6/1/2026, 12:00:00 AM',
                'ValidDateTo': '6/1/2026, 11:59:59 PM',
                'Reward': {},
                'DiscountMenuDisplayDetails': {'DiscountId': 0},
                # deliberately omit LocationRestrictions — the dispatch must
                # stamp it; if it ever stops, assertion (a) fails.
            }
            for ext in external_ids
        ],
    }


@tagged('post_install', '-at_install')
class TestDutchieMultistorePublish(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Submission = cls.env['mint.deal.submission']
        param = cls.env['ir.config_parameter'].sudo()
        param.set_param('dutchie.publish.mode', 'live')
        param.set_param('dutchie.publish.loc_ids', json.dumps({str(LSP): LOC_IDS}))
        param.set_param('dutchie.publish.api_key', 'test-key')
        param.set_param('dutchie.publish.url', 'http://invsvc.test')

    def _make_sub(self):
        return self.Submission.create({
            'name': 'Multistore Regression',
            'vendor_name': 'TestVendor',
            'discount_type': 'percent',
            'discount_value': 40.0,
        })

    def _publish(self, sub, build_result):
        """Run the live publish with _dutchie_build stubbed and urlopen mocked.
        Returns the list of decoded request bodies that were POSTed."""
        posted = []

        def _fake_urlopen(req, *args, **kwargs):
            posted.append(json.loads(req.data.decode()))
            return _FakeResp(json.dumps({'discount_id': 999}))

        with mock.patch.object(type(self.Submission), '_dutchie_build',
                               return_value=build_result), \
                mock.patch('urllib.request.urlopen', side_effect=_fake_urlopen):
            sub._dutchie_publish_after_convert()
        return posted

    # (a) + (b) for a single span ----------------------------------------
    def test_single_span_one_record_across_all_stores(self):
        sub = self._make_sub()
        posted = self._publish(sub, _build_result(['lgm_test_0']))

        # (b) one span -> exactly ONE POST, NOT one per store.
        self.assertEqual(len(posted), 1,
                         "single span must POST exactly one record, not one per store")
        body = posted[0]
        # owner LocId is non-zero (invsvc rejects locId == 0) and is loc_ids[0].
        self.assertEqual(body['locId'], LOC_IDS[0])
        self.assertNotEqual(body['locId'], 0)
        self.assertEqual(body['lspId'], LSP)
        # (a) every target store rides in LocationRestrictions.
        self.assertEqual(body['discount']['LocationRestrictions'], LOC_IDS)

        # idempotency map is {externalId: dutchieId}, one id per span.
        self.assertEqual(json.loads(sub.dutchie_publish_loc_ids),
                         {'lgm_test_0': 999})

    # (b) count tracks spans, not stores ---------------------------------
    def test_post_count_equals_spans_not_stores(self):
        sub = self._make_sub()
        ext_ids = ['lgm_test_0', 'lgm_test_1']  # two spans
        posted = self._publish(sub, _build_result(ext_ids))

        # Two spans across three stores: 2 POSTs — never 3 (per store) or
        # 6 (per span x store).
        self.assertEqual(len(posted), len(ext_ids),
                         "POST count must equal the number of spans, not stores")
        for body in posted:
            self.assertEqual(body['discount']['LocationRestrictions'], LOC_IDS)
            self.assertEqual(body['locId'], LOC_IDS[0])

        self.assertEqual(json.loads(sub.dutchie_publish_loc_ids),
                         {ext: 999 for ext in ext_ids})
