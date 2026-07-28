# -*- coding: utf-8 -*-
"""Customer push notifications on mint.pos.order state changes.

Every test here traces to a real incident or to a guard protecting one:

* Customers received TWO identical pushes per status change, because both
  order_api controllers re-sent the push that write() had already sent.
* /api/checkout/complete PUTs state='confirmed' unconditionally, so a retry
  re-notified for a transition that never happened.
* Orders observed in production flapping pickup -> online_orders -> pickup
  inside 60 seconds, producing three pushes for one real event.

The counterweight throughout is that over-suppression is worse than a
duplicate: a customer who never hears "Ready for Pickup" drives to the store
not knowing, so several tests assert that genuine forward milestones and
cancellations always get through.
"""
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestOrderNotifications(TransactionCase):

    def setUp(self):
        super().setUp()
        self.partner = self.env['res.partner'].create({
            'name': 'Push Test Customer',
            'email': 'push-test@example.invalid',
        })
        self.company = self.env.company

        # Record every push instead of hitting Apple/FCM.
        self.sent = []
        test_case = self

        def _fake_send(model_self, partner_id, title, body, url=None,
                       icon=None, image=None, actions=None, site_id=None):
            test_case.sent.append({
                'partner_id': partner_id, 'title': title, 'body': body,
            })
            return 1

        patcher = patch.object(
            type(self.env['mint.push.subscription']),
            'send_to_partner', _fake_send,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    # ── helpers ──────────────────────────────────────────────────────

    def _order(self, state='placed', partner=True):
        return self.env['mint.pos.order'].create({
            'partner_id': self.partner.id if partner else False,
            'company_id': self.company.id,
            'state': state,
        })

    def _set(self, order, state, **ctx):
        """State change through the same path every real caller uses."""
        order.with_context(from_dutchie_sync=True, **ctx).write({'state': state})

    def _titles(self):
        return [s['title'] for s in self.sent]

    # ── the original duplicate-push bug ──────────────────────────────

    def test_single_transition_sends_exactly_one_push(self):
        order = self._order('placed')
        self._set(order, 'confirmed')
        self.assertEqual(
            len(self.sent), 1,
            'One state change must produce exactly one push. Two means a '
            'second sender was reintroduced alongside the write() hook.',
        )
        self.assertEqual(self.sent[0]['partner_id'], self.partner.id)

    def test_noop_rewrite_sends_nothing(self):
        """/api/checkout/complete PUTs 'confirmed' unconditionally."""
        order = self._order('placed')
        self._set(order, 'confirmed')
        self.sent.clear()
        self._set(order, 'confirmed')
        self.assertEqual(
            self.sent, [],
            'Re-writing the state an order already has is not a transition '
            'and must not notify.',
        )

    # ── flapping / cooldown ──────────────────────────────────────────

    def test_production_flap_sequence_notifies_once(self):
        """Regression for the observed pickup -> online_orders -> pickup flap."""
        order = self._order('sales_floor')
        self._set(order, 'pickup')
        self._set(order, 'online_orders')   # backward: sync artifact
        self._set(order, 'pickup')          # same state again, inside cooldown
        self.assertEqual(
            len(self.sent), 1,
            'A lane flapping away and back is one real event. Got: %s'
            % self._titles(),
        )
        self.assertEqual(self.sent[0]['title'], 'Ready for Pickup!')

    def test_backward_move_is_suppressed(self):
        order = self._order('sales_floor')
        self._set(order, 'pickup')
        self.sent.clear()
        self._set(order, 'confirmed')
        self.assertEqual(
            self.sent, [],
            'Sliding backward through the pipeline is not a customer event.',
        )

    def test_same_state_after_cooldown_notifies_again(self):
        order = self._order('sales_floor')
        self._set(order, 'pickup')
        self._set(order, 'online_orders')
        self.sent.clear()
        # Age the stamp past the default 30-minute window.
        order.write({'last_push_at': fields.Datetime.now() - timedelta(hours=2)})
        self._set(order, 'pickup')
        self.assertEqual(
            len(self.sent), 1,
            'Once the cooldown lapses a genuine re-ready must reach the '
            'customer — the guard is a debounce, not a permanent mute.',
        )

    def test_cooldown_window_is_configurable(self):
        self.env['ir.config_parameter'].sudo().set_param(
            'mint_pos_bridge.push_cooldown_minutes', '0',
        )
        order = self._order('sales_floor')
        self._set(order, 'pickup')
        self._set(order, 'online_orders')
        self.sent.clear()
        self._set(order, 'pickup')
        self.assertEqual(
            len(self.sent), 1,
            'A cooldown of 0 disables the window entirely.',
        )

    def test_malformed_cooldown_param_falls_back_to_default(self):
        self.env['ir.config_parameter'].sudo().set_param(
            'mint_pos_bridge.push_cooldown_minutes', 'not-a-number',
        )
        order = self._order('sales_floor')
        self.assertEqual(order._push_cooldown_minutes(), 30)

    def test_negative_cooldown_param_falls_back_to_default(self):
        """A negative window would invert the comparison and mute forever."""
        self.env['ir.config_parameter'].sudo().set_param(
            'mint_pos_bridge.push_cooldown_minutes', '-5',
        )
        order = self._order('sales_floor')
        self.assertEqual(order._push_cooldown_minutes(), 30)

    # ── over-suppression guards ──────────────────────────────────────

    def test_forward_progression_notifies_every_milestone(self):
        order = self._order('placed')
        for state in ('confirmed', 'sales_floor', 'pickup'):
            self._set(order, state)
        self.assertEqual(
            len(self.sent), 3,
            'Each forward milestone is a distinct thing the customer needs '
            'to know. Got: %s' % self._titles(),
        )

    def test_cancellation_always_notifies_even_from_a_later_state(self):
        order = self._order('sales_floor')
        self._set(order, 'pickup')
        self.sent.clear()
        self._set(order, 'cancelled')
        self.assertEqual(
            len(self.sent), 1,
            'Cancellation outranks every state — it must never be filtered '
            'as a backward move.',
        )
        self.assertEqual(self.sent[0]['title'], 'Order Cancelled')

    def test_failed_send_does_not_start_a_cooldown(self):
        """A push that raised was never delivered, so nothing is stamped."""
        def _boom(model_self, partner_id, title, body, url=None, icon=None,
                  image=None, actions=None, site_id=None):
            raise RuntimeError('push provider down')

        order = self._order('placed')
        with patch.object(type(self.env['mint.push.subscription']),
                          'send_to_partner', _boom):
            self._set(order, 'confirmed')
        self.assertFalse(
            order.last_push_state,
            'Stamping on failure would start a cooldown for a notification '
            'the customer never received.',
        )

    # ── existing suppression contracts ───────────────────────────────

    def test_silent_context_suppresses(self):
        order = self._order('placed')
        self._set(order, 'cancelled', mint_pos_silent=True)
        self.assertEqual(
            self.sent, [],
            'Auto-cancelling an abandoned order must not tell the customer.',
        )

    def test_order_without_partner_never_notifies(self):
        order = self._order('placed', partner=False)
        self._set(order, 'confirmed')
        self.assertEqual(self.sent, [])

    def test_unnotified_state_sends_nothing(self):
        order = self._order('placed')
        self._set(order, 'processing')  # no message defined for this state
        self.assertEqual(self.sent, [])

    # ── bookkeeping ──────────────────────────────────────────────────

    def test_stamps_reflect_what_the_customer_last_heard(self):
        order = self._order('sales_floor')
        self._set(order, 'pickup')
        self._set(order, 'online_orders')  # suppressed
        self.assertEqual(
            order.state, 'online_orders',
            'The order still tracks its true state...',
        )
        self.assertEqual(
            order.last_push_state, 'pickup',
            '...while the stamp records the last thing actually sent.',
        )
        self.assertTrue(order.last_push_at)
