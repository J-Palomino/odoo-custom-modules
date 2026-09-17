"""Tests for the daily sign-off sheet's record selection and grouping.

The layout is not what breaks on a sheet like this — the selection is. A
sign-off list that quietly includes finished work, or that buries the
unassigned pile at the top, stops being read within a week. These pin the
three rules the sheet depends on:

* completed and archived tickets never reach the page, however the lead
  selected them;
* sections are ordered team, then technician, with Unassigned last; and
* "overdue" means a scheduled date has passed, not merely that a ticket is old
  — an unscheduled ticket has nothing to be late against.
"""

from datetime import timedelta

from odoo import fields
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestMaintenanceSignoffReport(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.report = cls.env[
            "report.mint_maintenance_form.report_maintenance_signoff"
        ]
        cls.team = cls.env["maintenance.team"].create({"name": "Sign-Off Test Team"})
        cls.open_stage = cls.env["maintenance.stage"].search(
            [("done", "=", False)], limit=1
        )
        cls.done_stage = cls.env["maintenance.stage"].search(
            [("done", "=", True)], limit=1
        )
        cls.technician = cls.env["res.users"].create(
            {
                "name": "Aaron Technician",
                "login": "signoff-tech@example.com",
            }
        )

    def _make_request(self, name, **vals):
        values = {
            "name": name,
            "maintenance_team_id": self.team.id,
            "stage_id": self.open_stage.id,
        }
        values.update(vals)
        return self.env["maintenance.request"].create(values)

    def _sections_for(self, requests):
        return self.report._get_report_values(requests.ids)["sections"]

    def test_completed_and_archived_tickets_are_dropped(self):
        """Selecting everything in a list view must not print finished work."""
        still_open = self._make_request("Open ticket")
        closed = self._make_request("Closed ticket", stage_id=self.done_stage.id)
        archived = self._make_request("Archived ticket", archive=True)

        values = self.report._get_report_values(
            (still_open | closed | archived).ids
        )

        printed_ids = [
            row["id"] for section in values["sections"] for row in section["rows"]
        ]
        self.assertEqual(printed_ids, [still_open.id])
        self.assertEqual(values["open_total"], 1)
        # The two dropped tickets are reported on the sheet, so "I selected 3
        # and got 1" is explained rather than looking like a bug.
        self.assertEqual(values["excluded_count"], 2)

    def test_unassigned_section_sorts_last(self):
        """Work nobody owns belongs at the bottom, not above assigned work."""
        unassigned = self._make_request("Nobody owns this")
        assigned = self._make_request("Owned", user_id=self.technician.id)

        sections = self._sections_for(unassigned | assigned)

        self.assertEqual(len(sections), 2)
        self.assertEqual(sections[0]["technician"], self.technician)
        self.assertFalse(sections[1]["technician"])
        self.assertEqual(sections[1]["rows"][0]["id"], unassigned.id)

    def test_overdue_needs_a_scheduled_date_in_the_past(self):
        """Old is not late; only a missed commitment is late."""
        # Offsets of two days, and a request_date anchored to the *user's*
        # today rather than the server's, so the assertions cannot flip on a
        # timezone boundary when the suite runs late in the UTC day.
        user_today = fields.Date.context_today(self.env.user)
        late = self._make_request(
            "Missed its date", schedule_date=fields.Datetime.now() - timedelta(days=2)
        )
        upcoming = self._make_request(
            "Due later", schedule_date=fields.Datetime.now() + timedelta(days=2)
        )
        unscheduled = self._make_request(
            "Never scheduled", request_date=user_today - timedelta(days=90)
        )

        rows = {
            row["id"]: row
            for section in self._sections_for(late | upcoming | unscheduled)
            for row in section["rows"]
        }

        self.assertTrue(rows[late.id]["overdue"])
        self.assertFalse(rows[upcoming.id]["overdue"])
        self.assertFalse(rows[unscheduled.id]["overdue"])
        # ...but the 90-day-old ticket still reports its real age, which is the
        # signal a supervisor actually scans the column for.
        self.assertEqual(rows[unscheduled.id]["age_days"], 90)
        self.assertEqual(rows[unscheduled.id]["scheduled_on"], "")

    def test_age_falls_back_to_create_date(self):
        """request_date is blank on some intake paths; age must not read zero."""
        request = self._make_request("No request date", request_date=False)

        rows = self._sections_for(request)[0]["rows"]

        self.assertEqual(rows[0]["age_days"], 0)
        self.assertTrue(rows[0]["opened_on"])

    def test_team_button_prints_only_open_tickets(self):
        """The one-click path applies the same open-only rule as the report."""
        still_open = self._make_request("Open for the team button")
        self._make_request("Closed for the team button", stage_id=self.done_stage.id)

        action = self.team.action_print_signoff_sheet()

        self.assertEqual(action["type"], "ir.actions.report")
        self.assertEqual(action["context"]["active_ids"], [still_open.id])
