# -*- coding: utf-8 -*-
"""A promo issued in the Arizona evening must be spendable that same evening.

`context_today(user)` resolves to the UTC date for a portal user with no tz,
and from 17:00 MST the UTC date is already tomorrow. The issue endpoint used to
write that straight into `valid_from`, so Dutchie got ValidDateFrom = tomorrow
and the register refused the code until local midnight. Hit live 2026-09-18:
two $250 promos issued at 21:14 MST carried ValidDateFrom 9/19.

The window helper is pure on purpose — the bug is date arithmetic, and pinning
it here needs no JWT, no request and no Dutchie.
"""
from datetime import date

from odoo.tests.common import BaseCase
from odoo.addons.mint_customer_api.controllers import customer as customer_mod


class TestPromoValidityWindow(BaseCase):

    def test_evening_issue_is_valid_on_the_local_day(self):
        # 21:14 MST on 9/18 is 04:14 UTC on 9/19: `today` arrives as 9/19 while
        # the register is still on 9/18.
        utc_today = date(2026, 9, 19)
        local_day_at_register = date(2026, 9, 18)
        valid_from, _until = customer_mod._promo_validity_window(utc_today, 30)
        self.assertLessEqual(valid_from, local_day_at_register)

    def test_end_of_window_is_not_pulled_in(self):
        # Opening a day early must not cost the holder a day at the far end.
        _from, valid_until = customer_mod._promo_validity_window(date(2026, 9, 19), 30)
        self.assertEqual(valid_until, date(2026, 10, 19))

    def test_slack_is_at_least_one_day(self):
        # UTC runs at most a calendar day ahead of any US store; less than one
        # day of slack reopens the bug.
        self.assertGreaterEqual(customer_mod.PROMO_VALID_FROM_SLACK_DAYS, 1)
