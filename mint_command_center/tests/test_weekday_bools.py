"""Regression: PTL-day → Dutchie weekday reduction.

Guards two silent-corruption bugs in the path-2 day-of-week logic:
  #6 — a deal whose plotted days fall outside the discount's validity span (e.g.
       entirely >60 days out) collapsed to all-False, which Dutchie reads as
       "active EVERY day".
  #7 — weekdays were unioned across markets, so a deal plotted Mon-in-AZ and
       Fri-in-MO published BOTH days to BOTH states.

Exercises the pure reducer ``weekday_bools_from_days`` (the same logic
``mint.discount._compute_weekday_bools`` and the Dutchie payload builder use),
so no Odoo fixtures are needed.
"""
from datetime import date

from odoo.tests import TransactionCase, tagged

from ..models.deal_mixins import weekday_bools_from_days, DAY_FIELD_NAMES

MON = date(2026, 6, 22)   # Monday
TUE = date(2026, 6, 23)   # Tuesday
FRI = date(2026, 6, 19)   # Friday
AZ, MO = 1, 2             # market ids


def _true_days(bools):
    return {k for k, v in bools.items() if v}


@tagged('post_install', '-at_install')
class TestWeekdayBools(TransactionCase):

    def test_basic_mapping(self):
        b = weekday_bools_from_days([(MON, AZ), (FRI, AZ)])
        self.assertEqual(_true_days(b), {'monday', 'friday'})
        self.assertEqual(set(b), set(DAY_FIELD_NAMES))  # all 7 keys present

    def test_span_excludes_out_of_range(self):
        # FRI (6/19) is before the span -> dropped; only TUE remains.
        b = weekday_bools_from_days([(FRI, AZ), (TUE, AZ)],
                                    span_from=date(2026, 6, 22),
                                    span_to=date(2026, 6, 30))
        self.assertEqual(_true_days(b), {'tuesday'})

    def test_all_false_when_everything_out_of_span(self):
        # #6: a deal whose only day is far outside its validity span resolves to
        # all-False — callers must treat this as refuse-to-publish, not every-day.
        b = weekday_bools_from_days([(MON, AZ)],
                                    span_from=date(2026, 9, 1),
                                    span_to=date(2026, 9, 30))
        self.assertEqual(_true_days(b), set())

    def test_market_scope_no_contamination(self):
        # #7: Mon-in-AZ + Fri-in-MO must not leak across markets.
        days = [(MON, AZ), (FRI, MO)]
        self.assertEqual(_true_days(weekday_bools_from_days(days, market_id=AZ)),
                         {'monday'})
        self.assertEqual(_true_days(weekday_bools_from_days(days, market_id=MO)),
                         {'friday'})
        # market-blind union (storefront/Redis path) keeps both
        self.assertEqual(_true_days(weekday_bools_from_days(days)),
                         {'monday', 'friday'})

    def test_none_date_skipped(self):
        b = weekday_bools_from_days([(None, AZ), (MON, AZ)])
        self.assertEqual(_true_days(b), {'monday'})

    def test_empty(self):
        self.assertEqual(_true_days(weekday_bools_from_days([])), set())
