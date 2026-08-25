"""
Regression tests for the PTL daily lifecycle cron.

Background: "PTL: Daily Deal Lifecycle" is the only thing that unpublishes a
PTL discount once valid_until has passed (cron_expire_past_deals flips
mint.ptl.deal.state and never touches mint.discount.is_published). It was
created solely by hooks.py post_init_hook — install-only, and guarded by a
name lookup that ignored `active` — so when it was switched off on production
on 2026-06-18 nothing could turn it back on. Two months later 329 PTL
discounts were still published past their valid_until, 96 of them still
computing is_active=True.

Two things are locked in here: the cron behaves correctly, and it exists as an
upgrade-restorable data record rather than a hook artefact.
"""
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

CRON_XMLID = 'mint_command_center.ir_cron_ptl_daily_lifecycle'


@tagged("post_install", "-at_install")
class TestPtlLifecycleCron(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Discount = cls.env['mint.discount']
        cls.today = fields.Date.today()

    def _discount(self, name, valid_from, valid_until, source='ptl', published=True):
        """A PTL discount with every weekday set, so is_active turns purely on
        is_published and the validity window."""
        return self.Discount.create({
            'name': name,
            'source': source,
            'discount_type': 'percent',
            'discount_amount': 0.3,
            'is_published': published,
            'valid_from': valid_from,
            'valid_until': valid_until,
            'monday': True, 'tuesday': True, 'wednesday': True, 'thursday': True,
            'friday': True, 'saturday': True, 'sunday': True,
        })

    def _run_cron(self):
        """Run the lifecycle, stubbing the outbound webhook.

        _push_discounts_to_redis fires urllib requests at the inventory
        service; a test must never do that.
        """
        Day = type(self.env['mint.ptl.day'])
        with patch.object(Day, '_push_discounts_to_redis', lambda self, ids: None):
            self.env['mint.discount']._cron_ptl_daily_lifecycle()

    # ── behaviour ────────────────────────────────────────────────────────

    def test_expired_ptl_discount_is_unpublished(self):
        """The bug: a discount whose window closed stays published forever."""
        d = self._discount(
            'LIFECYCLE expired',
            self.today - timedelta(days=60),
            self.today - timedelta(days=30),
        )
        self.assertTrue(d.is_published)
        self._run_cron()
        self.assertFalse(
            d.is_published,
            'a PTL discount past valid_until must be unpublished by the cron')
        self.assertFalse(
            d.is_active,
            'is_active is a stored compute on is_published — it must follow')

    def test_in_window_discount_stays_published(self):
        d = self._discount(
            'LIFECYCLE current',
            self.today - timedelta(days=1),
            self.today + timedelta(days=30),
        )
        self._run_cron()
        self.assertTrue(d.is_published, 'a live deal must not be unpublished')

    def test_discount_becomes_published_on_valid_from(self):
        d = self._discount(
            'LIFECYCLE due',
            self.today - timedelta(days=1),
            self.today + timedelta(days=10),
            published=False,
        )
        self._run_cron()
        self.assertTrue(d.is_published, 'a deal whose window opened must publish')

    def test_non_ptl_discounts_are_left_alone(self):
        """The cron is scoped to source='ptl'; Dutchie-sourced rows are not its
        business and have their own lifecycle."""
        d = self._discount(
            'LIFECYCLE dutchie expired',
            self.today - timedelta(days=60),
            self.today - timedelta(days=30),
            source='dutchie',
        )
        self._run_cron()
        self.assertTrue(
            d.is_published,
            'a non-PTL discount must not be touched by the PTL lifecycle cron')

    def test_open_ended_discount_is_never_expired(self):
        d = self._discount(
            'LIFECYCLE open ended',
            self.today - timedelta(days=60),
            False,
        )
        self._run_cron()
        self.assertTrue(
            d.is_published, 'valid_until=False means no end date, not expired')

    # ── wiring ───────────────────────────────────────────────────────────

    def test_cron_is_an_upgrade_restorable_data_record(self):
        """The actual fix. Before 19.0.6.71.0 this cron existed only as a
        post_init_hook artefact with no external id, so an upgrade could not
        restore it after someone switched it off. Fails on the old code."""
        cron = self.env.ref(CRON_XMLID, raise_if_not_found=False)
        self.assertTrue(
            cron,
            'the lifecycle cron must be an XML data record so -u can restore it')
        self.assertTrue(cron.active, 'the lifecycle cron must ship enabled')
        self.assertEqual(cron.code.strip(), 'model._cron_daily_lifecycle()')
        self.assertEqual(cron.model_id.model, 'mint.ptl.day')

    def test_cron_is_not_duplicated_by_the_migration(self):
        """The 19.0.6.71.0 pre-migration adopts the pre-existing cron instead
        of letting the data load create a second one."""
        crons = self.env['ir.cron'].with_context(active_test=False).search(
            [('name', '=', 'PTL: Daily Deal Lifecycle')])
        self.assertEqual(
            len(crons), 1,
            'exactly one lifecycle cron must exist, found %d' % len(crons))
