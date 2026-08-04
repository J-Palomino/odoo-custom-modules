"""Tests for the Fix-It form's pre-submittal duplicate guard.

Background: the intake form had no defence against repeat submission at any
layer, so one report could file an unbounded number of identical tickets —
worst observed case, the same AZ-Mesa door report filed 10 times in 57
seconds, and MR-1194/1195 five minutes apart.

These tests pin the two subtle failure modes that made earlier guards useless:

* keying on ``name``, which a Maintenance UI automation rewrites to
  ``MR-<id> - ...`` immediately after create, so the submitted title never
  matches what got stored; and
* keying on ``owner_user_id``, which is *computed* from ``employee_id`` and is
  therefore empty on 51% of production web-intake tickets regardless of what
  the controller puts in vals.

Both are regression tests: they fail against the guard as originally written.
"""

from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.tests import TransactionCase, tagged

from odoo.addons.mint_maintenance_form.controllers import main as form_main


class _FakeRequest:
    """Minimal stand-in for ``odoo.http.request``.

    ``_find_recent_duplicate`` only needs ``env`` — for the ORM, for the
    cursor the advisory lock runs on, and for the acting uid — so the guard
    can be exercised directly without booting the HTTP stack.
    """

    def __init__(self, env):
        self.env = env


@tagged("post_install", "-at_install")
class TestDuplicateGuard(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.controller = form_main.MaintenanceFormController()

        cls.company = cls.env["res.company"].create({"name": "Dup Guard Store"})
        cls.other_company = cls.env["res.company"].create({"name": "Dup Guard Store 2"})
        # Teams are company-less in production so one team serves every store;
        # requests then carry the company. Pinned explicitly here because
        # maintenance.team otherwise defaults to the acting company, which
        # would trip _check_company against a request in another company.
        cls.team = cls.env["maintenance.team"].create(
            {"name": "Dup Guard Team", "company_id": False}
        )
        cls.other_team = cls.env["maintenance.team"].create(
            {"name": "Dup Guard Team 2", "company_id": False}
        )

        cls.stage_open = cls.env["maintenance.stage"].create(
            {"name": "Dup Guard New", "sequence": 0, "done": False}
        )
        cls.stage_done = cls.env["maintenance.stage"].create(
            {"name": "Dup Guard Closed", "sequence": 90, "done": True}
        )

        internal = cls.env.ref("base.group_user").id
        companies = [(6, 0, [cls.company.id, cls.other_company.id])]
        # Odoo 19 renamed groups_id -> group_ids.
        cls.user_a = cls.env["res.users"].create({
            "name": "Dup Guard A",
            "login": "dupguard-a@example.com",
            "email": "dupguard-a@example.com",
            "group_ids": [(6, 0, [internal])],
            "company_id": cls.company.id,
            "company_ids": companies,
        })
        cls.user_b = cls.env["res.users"].create({
            "name": "Dup Guard B",
            "login": "dupguard-b@example.com",
            "email": "dupguard-b@example.com",
            "group_ids": [(6, 0, [internal])],
            "company_id": cls.company.id,
            "company_ids": companies,
        })

        cls.description = (
            "<p><strong>Submitted by:</strong> Douglas Schwartz</p>"
            "<p><strong>Email:</strong> dschwartz@letsgomint.com</p>"
            "<p><strong>Store:</strong> AZ - Tempe</p>"
            "<br/>add this script to the shop.letsgomint site for ahrefs"
        )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _vals(self, **overrides):
        """The vals dict the controller builds for a form submission."""
        vals = {
            "name": "Dup Guard Store - Websites - add this script",
            "description": self.description,
            "maintenance_type": "corrective",
            "maintenance_team_id": self.team.id,
            "company_id": self.company.id,
            "stage_id": self.stage_open.id,
            "email_cc": "dschwartz@letsgomint.com",
        }
        vals.update(overrides)
        return vals

    def _submit(self, user, **overrides):
        """Create a ticket the way the controller does: sudo, acting uid intact."""
        vals = self._vals(**overrides)
        # The controller also puts owner_user_id in vals; the ORM discards it
        # because the field is computed from employee_id. Included here so the
        # tests exercise the real payload.
        vals["owner_user_id"] = user.id
        fake = _FakeRequest(self.env(user=user))
        with patch.object(form_main, "request", fake):
            vals["x_submission_key"] = self.controller._submission_key(vals)
        return (
            self.env["maintenance.request"]
            .with_user(user)
            .sudo()
            .create(vals)
        )

    def _find(self, user, **overrides):
        """Run the guard as `user` would on a fresh submission."""
        vals = self._vals(**overrides)
        vals["owner_user_id"] = user.id
        fake = _FakeRequest(self.env(user=user))
        with patch.object(form_main, "request", fake):
            return self.controller._find_recent_duplicate(vals)

    def _backdate(self, record, seconds):
        """Push a record's create_date into the past (it is readonly via ORM)."""
        old = fields.Datetime.now() - timedelta(seconds=seconds)
        self.env.cr.execute(
            "UPDATE maintenance_request SET create_date = %s WHERE id = %s",
            (fields.Datetime.to_string(old), record.id),
        )
        record.invalidate_recordset(["create_date"])

    # ------------------------------------------------------------------
    # the guard fires
    # ------------------------------------------------------------------
    def test_identical_resubmission_is_caught(self):
        first = self._submit(self.user_a)
        self.assertEqual(self._find(self.user_a), first)

    def test_survives_name_rewrite_by_automation(self):
        """Regression: a guard keyed on `name` can never match.

        Production stamps `MR-<id> - <company> - <equipment> - <summary>` onto
        `name` right after create — it embeds the record's own id, so no two
        rows can share a title. Matching must key on `description`.
        """
        first = self._submit(self.user_a)
        first.sudo().write({"name": f"MR-{first.id} - Dup Guard Store - Websites"})
        self.assertNotEqual(first.name, self._vals()["name"])
        self.assertEqual(self._find(self.user_a), first)

    def test_matches_when_owner_user_id_is_empty(self):
        """Regression: the guard must not be scoped by owner_user_id.

        In production `hr_maintenance` redefines owner_user_id as
        compute='_compute_owner', store=True, driven by employee_id — so the
        value the controller puts in vals never reaches the column, and it is
        empty on 51% of web-intake tickets (89 of 174). MR-1194 stored False
        while MR-1195 stored 1985: the same person submitting the same form
        five minutes apart, which a guard scoped by owner_user_id could never
        have linked.

        hr_maintenance is not a declared dependency of this module, so vanilla
        Odoo accepts the value instead. The production shape is reproduced by
        clearing the column after create, which keeps the test meaningful in
        both environments.
        """
        first = self._submit(self.user_a)
        first.sudo().write({"owner_user_id": False})
        self.assertFalse(first.owner_user_id)
        self.assertEqual(self._find(self.user_a), first)

    def test_guard_survives_description_rewrite(self):
        """Regression: the guard must not depend on `description` equality.

        description is an HTML field with sanitize=True, so Odoo rewrites the
        markup on write and the stored value is not byte-identical to what was
        posted — an exact `description =` term silently never matches. The key
        is stamped at create instead, so neither the sanitiser nor a later
        edit by a technician can unmatch it.
        """
        first = self._submit(self.user_a)
        first.sudo().write({"description": "<p>tidied up by a technician</p>"})
        self.assertEqual(self._find(self.user_a), first)

    def test_second_duplicate_still_resolves_to_the_first(self):
        """A third click resolves to the original, not to the second copy."""
        first = self._submit(self.user_a)
        self.assertEqual(self._find(self.user_a), first)
        self.assertEqual(self._find(self.user_a), first)

    # ------------------------------------------------------------------
    # the guard stays out of the way
    # ------------------------------------------------------------------
    def test_different_description_is_not_a_duplicate(self):
        self._submit(self.user_a)
        self.assertIsNone(self._find(self.user_a, description="<p>something else</p>"))

    def test_outside_the_window_is_not_a_duplicate(self):
        first = self._submit(self.user_a)
        self._backdate(first, form_main.DUPLICATE_WINDOW_SECONDS + 60)
        self.assertIsNone(self._find(self.user_a))

    def test_inside_the_window_is_still_a_duplicate(self):
        first = self._submit(self.user_a)
        self._backdate(first, form_main.DUPLICATE_WINDOW_SECONDS - 60)
        self.assertEqual(self._find(self.user_a), first)

    def test_different_team_is_not_a_duplicate(self):
        self._submit(self.user_a)
        self.assertIsNone(
            self._find(self.user_a, maintenance_team_id=self.other_team.id)
        )

    def test_different_company_is_not_a_duplicate(self):
        self._submit(self.user_a)
        self.assertIsNone(
            self._find(self.user_a, company_id=self.other_company.id)
        )

    def test_different_submitter_is_not_a_duplicate(self):
        """Two people independently reporting the same thing get two tickets."""
        self._submit(self.user_b)
        self.assertIsNone(self._find(self.user_a))

    def test_closed_ticket_does_not_swallow_a_re_report(self):
        """If it was closed and they are submitting again, it is not fixed."""
        first = self._submit(self.user_a)
        first.sudo().write({"stage_id": self.stage_done.id})
        self.assertIsNone(self._find(self.user_a))

    def test_missing_description_returns_none(self):
        self._submit(self.user_a)
        self.assertIsNone(self._find(self.user_a, description=""))
