"""Regression tests for the non-contiguous schedule split in the Dutchie
publish path (dutchie_publish.py).

Focuses on `_partition_faithful` — the pure date-grouping logic that decides
how many separate Dutchie discounts a schedule produces. A "faithful" group is
one whose [min, max] range + the union of its weekdays reproduces EXACTLY the
group's dates; an unbroken weekly recurrence must stay a single group, while a
real calendar gap must force a split (otherwise Dutchie would activate the
discount on un-plotted days inside the gap).
"""
from datetime import date, timedelta

from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestDutchiePublishSplit(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Submission = cls.env["mint.deal.submission"]

    # ── helpers ──────────────────────────────────────────────────────────

    def _partition(self, dates):
        return self.Submission._partition_faithful(sorted(set(dates)))

    @staticmethod
    def _is_faithful(group):
        """A group is faithful iff expanding [min,max] over its weekday union
        reproduces exactly the group's dates."""
        lo, hi = group[0], group[-1]
        dows = {d.weekday() for d in group}
        expanded, cur = [], lo
        while cur <= hi:
            if cur.weekday() in dows:
                expanded.append(cur)
            cur += timedelta(days=1)
        return expanded == group

    @staticmethod
    def _thu_fri_sat(start_thu, weeks):
        """Thu/Fri/Sat for `weeks` consecutive weeks from a Thursday."""
        out = []
        for w in range(weeks):
            base = start_thu + timedelta(days=7 * w)
            out += [base, base + timedelta(days=1), base + timedelta(days=2)]
        return out

    def _assert_clean_partition(self, dates, expected_groups):
        dates = sorted(set(dates))
        groups = self._partition(dates)
        # every input date is covered exactly once, in order
        self.assertEqual([d for g in groups for d in g], dates,
                         "partition must cover every date exactly once, in order")
        # each group is individually faithful (no gap leakage)
        for g in groups:
            self.assertTrue(self._is_faithful(g),
                            f"group {g[0]}..{g[-1]} is not faithful")
        self.assertEqual(len(groups), expected_groups)
        return groups

    # ── cases ────────────────────────────────────────────────────────────

    def test_contiguous_weekly_recurrence_stays_one_group(self):
        """Real-world #388 shape: every Thu/Fri/Sat for 13 weeks is faithful as
        a single span and must NOT split."""
        groups = self._assert_clean_partition(
            self._thu_fri_sat(date(2026, 7, 2), 13), 1)
        self.assertEqual(groups[0][0], date(2026, 7, 2))
        self.assertEqual(groups[0][-1], date(2026, 9, 26))

    def test_gap_forces_split_no_leakage(self):
        """Early July + late September (skipping August) must become two
        faithful spans — August is never covered."""
        groups = self._assert_clean_partition(
            self._thu_fri_sat(date(2026, 7, 2), 1)
            + self._thu_fri_sat(date(2026, 9, 24), 1), 2)
        covered = {d for g in groups for d in g}
        august = {date(2026, 8, 1) + timedelta(days=i) for i in range(31)}
        self.assertFalse(covered & august, "no August date may be covered")

    def test_isolated_days_each_their_own_group(self):
        self._assert_clean_partition(
            [date(2026, 7, 2), date(2026, 8, 15), date(2026, 9, 30)], 3)

    def test_fully_contiguous_daily_run_is_one_group(self):
        self._assert_clean_partition(
            [date(2026, 7, 1) + timedelta(days=i) for i in range(10)], 1)

    def test_weekday_pattern_change_splits_to_stay_faithful(self):
        # Mon, Mon, Tue — adding the Tue would make the first Tue (un-plotted)
        # fall inside the range, so it must split rather than over-apply.
        self._assert_clean_partition(
            [date(2026, 7, 6), date(2026, 7, 13), date(2026, 7, 14)], 2)

    def test_single_date(self):
        self._assert_clean_partition([date(2026, 7, 2)], 1)

    def test_empty(self):
        self.assertEqual(self._partition([]), [])
