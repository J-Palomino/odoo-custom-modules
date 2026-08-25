"""
Regression tests for _push_discounts_to_redis thread usage.

On 2026-08-25 the restored PTL daily lifecycle cron ran for the first time in
two months and died with `RuntimeError: can't start new thread` at
ptl_day.py `thread.start()`. The method spawned one daemon thread per store,
and a backlog of ~600 discounts fanned across every store exhausted the
container's threads (OpenSSL failed in the same breath with "malloc failure"
and "ASN1 lib" errors). Because the exception escaped into
_cron_ptl_daily_lifecycle, Odoo rolled the transaction back and the 329
expired discounts the cron had just unpublished stayed published.

So: one thread per call, and a failure to start one must never propagate.
"""
from unittest.mock import patch
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.mint_command_center.models import ptl_day as ptl_day_module


@tagged("post_install", "-at_install")
class TestPtlWebhookDispatch(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Day = cls.env['mint.ptl.day']
        cls.discounts = cls.env['mint.discount'].create([
            {'name': 'WEBHOOK dispatch %d' % i, 'source': 'ptl',
             'discount_type': 'percent', 'discount_amount': 0.1}
            for i in range(2)
        ])
        # Far more stores than the old code could safely thread.
        cls.fake_map = {i: 'uuid-%04d-store' % i for i in range(1, 41)}

    def _push(self, thread_cls):
        """Run the push with a stubbed store map and a stand-in Thread class."""
        Day = type(self.Day)
        store_map = self.fake_map
        with patch.object(Day, '_get_store_uuid_map', lambda _self: store_map), \
                patch.object(ptl_day_module.threading, 'Thread', thread_cls):
            self.Day._push_discounts_to_redis(self.discounts.ids)

    def test_one_thread_per_call_regardless_of_store_count(self):
        """40 stores must still cost exactly one thread."""
        created = []

        class CountingThread:
            def __init__(self, target=None, daemon=None, **kw):
                self.target = target
                created.append(self)

            def start(self):
                pass

        self._push(CountingThread)
        self.assertEqual(
            len(created), 1,
            'expected a single dispatcher thread for %d stores, got %d'
            % (len(self.fake_map), len(created)))

    def test_thread_start_failure_does_not_propagate(self):
        """The bug: RuntimeError escaped and rolled back the cron."""
        sent = []

        class ExhaustedThread:
            def __init__(self, target=None, daemon=None, **kw):
                self.target = target

            def start(self):
                raise RuntimeError("can't start new thread")

        class FakeResp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            sent.append(req)
            return FakeResp()

        with patch.object(ptl_day_module.urllib.request, 'urlopen', fake_urlopen):
            # Must not raise.
            self._push(ExhaustedThread)

        self.assertEqual(
            len(sent), len(self.fake_map),
            'inline fallback should still deliver one request per store')

    def test_each_store_gets_its_own_location_id(self):
        """The old closure captured the loop variable, so every thread logged
        (and could mis-attribute) the last store."""
        import json

        seen = []

        class InlineThread:
            def __init__(self, target=None, daemon=None, **kw):
                self.target = target

            def start(self):
                self.target()

        class FakeResp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            seen.append(json.loads(req.data.decode())['location_id'])
            return FakeResp()

        with patch.object(ptl_day_module.urllib.request, 'urlopen', fake_urlopen):
            self._push(InlineThread)

        self.assertEqual(
            sorted(seen), sorted(self.fake_map.values()),
            'each store must receive a payload addressed to itself')
