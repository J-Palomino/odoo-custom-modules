"""Team-level entry point for the daily sign-off sheet."""

from odoo import _, models
from odoo.exceptions import UserError


class MaintenanceTeam(models.Model):
    _inherit = "maintenance.team"

    def action_print_signoff_sheet(self):
        """Print every still-open ticket owned by this team.

        The list-view Print entry already covers "print exactly what I
        filtered". This button covers the daily ritual, where the point is that
        nobody has to remember which combination of filters means "everything
        still open" — Closed is the only done stage, but that is a fact about
        the current stage configuration, not something a supervisor should have
        to know at 6am.

        The search runs as the current user deliberately, without sudo. Record
        rules then decide what lands on the page: rule 919 gives team members
        read on their own team's tickets, and the global multi-company rule 908
        scopes to their allowed companies. So a technician printing their team's
        sheet gets exactly the tickets Odoo would show them in the list view,
        and a manager printing the same team gets more. Sudo here would quietly
        turn a print button into a company-isolation bypass.
        """
        self.ensure_one()
        requests = self.env["maintenance.request"].search(
            [
                ("maintenance_team_id", "=", self.id),
                ("stage_id.done", "=", False),
                # `archive` is a plain boolean here, not the ORM active field,
                # so archived tickets come back from search unless excluded.
                ("archive", "=", False),
            ]
        )
        if not requests:
            raise UserError(
                _(
                    "%s has no open tickets you can see right now, so there is "
                    "nothing to sign off.",
                    self.name,
                )
            )
        # config=False is load-bearing, not tidying. report_action() returns
        # the *document layout configurator* instead of the report whenever the
        # caller is_admin() and the active company has no
        # external_report_layout_id -- and as of 2026-09-16 that is true of all
        # 70 companies in this database. Without this, every admin pressing a
        # daily print button would get a letterhead wizard instead of a sheet.
        # The list-view Print entry is unaffected (the web client executes the
        # report action directly and never reaches this check), so passing
        # config=False is also what keeps the two entry points behaving alike.
        return self.env.ref(
            "mint_maintenance_form.action_report_maintenance_signoff"
        ).report_action(requests, config=False)
