# -*- coding: utf-8 -*-
"""Query-time identity union on /orders (T3, Odoo task #109604).

One human holds several res.partner rows — a POS roster record, a web signup,
and a fragment per bare walk-in — and /orders filtered strictly on the single
partner the caller signed into. Measured on prod 2026-09-01: five partners
share one driver's licence with 88 orders split 81/7/0/0/0 between them, so
whichever record the customer logged into decided how much of their own
history they could see.

The risk running the other way is worse than the bug: widen the filter on a
weak signal and you put a stranger's purchases — and their medical status — on
someone else's screen. So every test here is really asking one of two
questions: does the union fire when it should, and does it refuse when there
is any doubt at all?
"""
from odoo.tests.common import TransactionCase, tagged

from ..controllers.order_api import _base_email


@tagged('post_install', '-at_install')
class TestBaseEmail(TransactionCase):
    """The mailbox comparison behind the claimed-account check.

    Four of the five partners sharing that licence on prod are '+alias'
    signups by one person (jpalomino@, jpalomino+1@, jpalomino123@,
    jpalomino+123@). Treating those as four different people would have made
    the union useless for exactly the case that motivates it, so the check
    compares the mailbox that actually receives the mail.
    """

    def test_plus_alias_is_the_same_mailbox(self):
        self.assertEqual(
            _base_email('jpalomino+123@brightroot.com'),
            _base_email('jpalomino@brightroot.com'),
        )

    def test_case_and_whitespace_are_not_identity(self):
        self.assertEqual(
            _base_email('  JPalomino+9@Brightroot.com '),
            _base_email('jpalomino@brightroot.com'),
        )

    def test_different_local_part_is_a_different_person(self):
        self.assertNotEqual(
            _base_email('someone.else@brightroot.com'),
            _base_email('jpalomino@brightroot.com'),
        )

    def test_same_local_part_different_domain_is_a_different_person(self):
        """The domain is load-bearing: '+' addressing only aliases within one."""
        self.assertNotEqual(
            _base_email('jpalomino@gmail.com'),
            _base_email('jpalomino@brightroot.com'),
        )

    def test_garbage_never_compares_equal_to_a_real_address(self):
        """A blank result must not make two unrelated partners look like one."""
        self.assertEqual(_base_email(''), '')
        self.assertEqual(_base_email(None), '')
        self.assertEqual(_base_email('not-an-address'), '')
        self.assertNotEqual(_base_email('not-an-address'),
                            _base_email('jpalomino@brightroot.com'))


@tagged('post_install', '-at_install')
class TestIdentityUnion(TransactionCase):
    """Which partners a caller's own /orders view is allowed to resolve to."""

    def setUp(self):
        super().setUp()
        self.Partner = self.env['res.partner']
        self.ICP = self.env['ir.config_parameter'].sudo()
        self.ICP.set_param('mint_pos_bridge.orders_identity_union', '1')
        self.ICP.set_param('mint_pos_bridge.orders_identity_union_max', '10')
        if 'x_dutchie_identity_key' not in self.Partner._fields:
            self.skipTest('x_dutchie_identity_key not installed (mint_dutchie_sync)')

    def _partner(self, name, key=False, email=False):
        return self.Partner.create({
            'name': name,
            'email': email,
            'x_dutchie_identity_key': key,
        })

    # -- the union fires -----------------------------------------------------

    def test_strong_key_unions_unclaimed_fragments(self):
        """A dl: key plus three keyless-login fragments resolves to all four."""
        me = self._partner('Me', 'dl:D0001', 'me@example.com')
        frags = [self._partner('Frag %s' % i, 'dl:D0001') for i in range(3)]
        ids = self._union(me)
        self.assertEqual(set(ids), {me.id} | {f.id for f in frags})

    def test_plus_alias_sibling_is_the_same_person(self):
        """The prod case: two web signups, one mailbox, one licence."""
        me = self._partner('Me', 'dl:D0002', 'juan+123@example.com')
        alias = self._partner('Me again', 'dl:D0002', 'juan@example.com')
        self._give_portal_login(alias, 'web:juan@example.com')
        self.assertIn(alias.id, self._union(me))

    # -- the union refuses ---------------------------------------------------

    def test_weak_name_dob_key_never_unions(self):
        """nd: keys include rows like 'nd:* *|08/28/1986' — far too weak."""
        me = self._partner('Me', 'nd:JOHN SMITH|01/01/1990')
        self._partner('Someone else', 'nd:JOHN SMITH|01/01/1990')
        self.assertEqual(self._union(me), [me.id])

    def test_phone_key_never_unions(self):
        """ph: keys are raw roster strings; 0000000000 is a real row on prod."""
        me = self._partner('Me', 'ph:0000000000')
        self._partner('Someone else', 'ph:0000000000')
        self.assertEqual(self._union(me), [me.id])

    def test_separately_claimed_account_is_excluded(self):
        """Same licence, different mailbox — two people, or a data error."""
        me = self._partner('Me', 'dl:D0003', 'me@example.com')
        other = self._partner('Not me', 'dl:D0003', 'stranger@example.com')
        self._give_portal_login(other, 'web:stranger@example.com')
        self.assertEqual(self._union(me), [me.id])

    def test_claimed_sibling_excluded_when_caller_has_no_email(self):
        """No mailbox to compare means no way to prove sameness — fail closed."""
        me = self._partner('Me', 'dl:D0004')
        other = self._partner('Not me', 'dl:D0004', 'stranger@example.com')
        self._give_portal_login(other, 'web:stranger2@example.com')
        self.assertEqual(self._union(me), [me.id])

    def test_sentinel_key_never_unions(self):
        """A key that is only its prefix is a placeholder, not an identity."""
        for sentinel in ('dl:', 'mj:', ''):
            me = self._partner('Me %s' % sentinel, sentinel)
            self._partner('Other %s' % sentinel, sentinel)
            self.assertEqual(self._union(me), [me.id], sentinel)

    def test_missing_key_never_unions(self):
        me = self._partner('Me', False)
        self.assertEqual(self._union(me), [me.id])

    def test_oversized_group_refuses_rather_than_trims(self):
        """A key held by an implausible number of people is suspect, not useful."""
        self.ICP.set_param('mint_pos_bridge.orders_identity_union_max', '3')
        me = self._partner('Me', 'dl:D0005', 'me@example.com')
        for i in range(5):
            self._partner('Frag %s' % i, 'dl:D0005')
        self.assertEqual(self._union(me), [me.id])

    def test_flag_off_is_previous_behaviour(self):
        self.ICP.set_param('mint_pos_bridge.orders_identity_union', '0')
        me = self._partner('Me', 'dl:D0006', 'me@example.com')
        self._partner('Frag', 'dl:D0006')
        self.assertEqual(self._union(me), [me.id])

    def test_staff_sibling_is_never_folded_in(self):
        """Staff who also shop must not have their record joined to a customer view."""
        me = self._partner('Me', 'dl:D0007', 'me@example.com')
        staff = self._partner('Staff', 'dl:D0007', 'me@example.com')
        self.env['res.users'].create({
            'name': 'Staff user', 'login': 'staff-union-test',
            'partner_id': staff.id,
            'group_ids': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self.assertEqual(self._union(me), [me.id])

    # -- helpers -------------------------------------------------------------

    def _give_portal_login(self, partner, login):
        return self.env['res.users'].with_context(no_reset_password=True).create({
            'name': partner.name, 'login': login, 'partner_id': partner.id,
            'group_ids': [(6, 0, [self.env.ref('base.group_portal').id])],
        })

    def _union(self, partner):
        """Call the controller helper with a request env bound to the test cursor."""
        from unittest.mock import patch
        from ..controllers import order_api

        class _Req:
            env = self.env

        with patch.object(order_api, 'request', _Req):
            return order_api._identity_union_partner_ids(partner.id)
