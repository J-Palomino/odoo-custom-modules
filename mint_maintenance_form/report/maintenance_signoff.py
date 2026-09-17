"""Report values for the maintenance daily sign-off sheet.

The sheet answers a question the Maintenance app cannot answer on paper today:
"what does this team still owe, and who signed for it?" Before this report,
``maintenance.request`` had **zero** ``ir.actions.report`` records, so the only
way off-screen was the generic xlsx export — which produces a spreadsheet, not
something a technician initials at the end of a shift.

Two design points that are easy to get wrong:

* **The sheet is always "still open", never "what you selected".** Leads print
  from a filtered list view, and "select all" cheerfully includes Closed
  tickets. Rendering those would produce a sign-off line for work that is
  already finished, which is exactly the kind of paperwork that teaches people
  to stop reading the paperwork. Completed and archived records are dropped
  here, and the count of what was dropped is printed on the sheet so a lead who
  selected 40 and got 31 sees why on the page.
* **Everything the template needs is computed here.** Ages, overdue flags and
  localised dates are resolved to primitives before they reach QWeb, so the
  template stays a layout and date arithmetic stays testable.
"""

import logging
import re

from odoo import api, fields, models
from odoo.tools import html2plaintext
from odoo.tools.misc import format_date, format_datetime

_logger = logging.getLogger(__name__)

# Sorts after every real name, so the "Unassigned" block lands at the bottom of
# the sheet. An empty string would sort it to the top, putting the work nobody
# owns above the work someone does.
_UNASSIGNED_SORT_KEY = "￿"

# A Maintenance UI automation rewrites `name` to "MR-<id> - <store> - <equipment>
# - <text>" shortly after create, so the number people say out loud already
# lives in the title.
_TICKET_REF_RE = re.compile(r"^\s*(MR-\d+)\s*-\s*(.*)$", re.DOTALL)

# html2plaintext renders the web form's "<strong>Label:</strong> value" preamble
# as "*Label:* value" lines.
_META_LINE_RE = re.compile(r"^\*\s*([^:*]+?)\s*:\s*\*\s*(.*)$")

# Enough to carry the reporter's actual complaint, short enough that a row stays
# a row. Engineering alone has 230 open tickets; untruncated descriptions would
# turn a shift sheet into a booklet.
_DETAILS_MAX_CHARS = 240


class ReportMaintenanceSignoff(models.AbstractModel):
    # Must be report.<module>.<template id> — this is how the report engine
    # finds the parser for report_name "mint_maintenance_form.report_maintenance_signoff".
    _name = "report.mint_maintenance_form.report_maintenance_signoff"
    _description = "Maintenance Daily Sign-Off Sheet"

    @api.model
    def _get_report_values(self, docids, data=None):
        requests = self.env["maintenance.request"].browse(docids).exists()

        # `archive` is a plain boolean on maintenance.request, not the ORM's
        # active field, so archived tickets are NOT filtered out by the browse
        # above and have to be excluded explicitly.
        open_requests = requests.filtered(
            lambda request: not request.stage_id.done and not request.archive
        )
        excluded_count = len(requests) - len(open_requests)

        # Read the labels off the field rather than hardcoding "Very Low/Low/
        # Normal/High": the selection is base maintenance's, and if it ever
        # changes the sheet should follow it instead of printing stale words.
        priority_labels = dict(
            self.env["maintenance.request"]
            ._fields["priority"]
            ._description_selection(self.env)
        )
        today = fields.Date.context_today(self)

        buckets = {}
        for request in open_requests:
            key = (request.maintenance_team_id, request.user_id)
            buckets[key] = buckets.get(key, self.env["maintenance.request"]) | request

        sections = []
        for (team, technician), bucket in sorted(
            buckets.items(),
            key=lambda item: (
                item[0][0].name or _UNASSIGNED_SORT_KEY,
                item[0][1].name or _UNASSIGNED_SORT_KEY,
            ),
        ):
            rows = [
                self._signoff_row(request, priority_labels, today)
                for request in self._sort_requests(bucket)
            ]
            sections.append(
                {
                    "team": team,
                    "technician": technician,
                    "rows": rows,
                    "overdue_count": sum(1 for row in rows if row["overdue"]),
                    "blocked_count": sum(1 for row in rows if row["blocked"]),
                }
            )

        return {
            "doc_ids": open_requests.ids,
            "doc_model": "maintenance.request",
            "docs": open_requests,
            "sections": sections,
            # web.external_layout resolves the letterhead from `o` or `company`;
            # this sheet spans companies (one team works tickets for many
            # stores), so pin the header to the user's active company rather
            # than letting it fall through to whichever record renders first.
            "company": self.env.company,
            "excluded_count": excluded_count,
            "open_total": len(open_requests),
            "printed_by": self.env.user,
            # format_datetime localises to the user's timezone. The Odoo
            # container runs UTC, so a raw now() would print tomorrow's date to
            # anyone printing an evening sheet in Phoenix.
            "printed_on": format_datetime(self.env, fields.Datetime.now()),
        }

    @api.model
    def _sort_requests(self, requests):
        """Highest priority first, then oldest first within a priority band.

        Oldest-first is deliberate: a sign-off sheet is a nag list, and the
        tickets most at risk of being quietly abandoned are the old ones.
        """
        return requests.sorted(
            key=lambda request: (
                -int(request.priority or "0"),
                self._opened_on(request) or fields.Date.today(),
                request.id,
            )
        )

    @api.model
    def _opened_on(self, request):
        """Date the ticket entered the queue.

        ``request_date`` is user-editable and blank on some intake paths, so
        fall back to the create stamp rather than reporting an age of zero for
        a ticket that has been sitting for a month.
        """
        if request.request_date:
            return request.request_date
        if request.create_date:
            return fields.Datetime.context_timestamp(request, request.create_date).date()
        return False

    @api.model
    def _split_ticket_name(self, request):
        """Return ``(ticket_ref, subject)`` for a request.

        The ticket number people quote is ``MR-<id>``, and it is already baked
        into ``name`` by the Maintenance automation. Pull it into its own column
        and strip it from the subject, rather than printing "#1511" next to
        "MR-1511 - ...". Tickets the automation has not touched yet fall back to
        deriving the ref from the id, so the column is never blank.
        """
        match = _TICKET_REF_RE.match(request.name or "")
        if match:
            return match.group(1), match.group(2).strip()
        return "MR-%s" % request.id, (request.name or "").strip()

    @api.model
    def _describe(self, request):
        """Return ``(reported_by, details)`` from the HTML description.

        Web-form tickets open with a metadata block (Submitted by / Email /
        State / Store / Equipment) that ``html2plaintext`` renders as
        ``*Label:* value`` lines. State and Store duplicate columns the sheet
        already carries and Email is noise on paper, so the block is parsed out:
        the submitter earns a line of its own, the rest is dropped, and what
        survives is the reporter's actual account of the problem. Phone, SMS and
        hand-entered tickets have no such block and pass through untouched.
        """
        text = html2plaintext(request.description or "")
        reported_by = ""
        narrative = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            meta = _META_LINE_RE.match(line)
            if meta:
                if meta.group(1).strip().lower() == "submitted by":
                    reported_by = meta.group(2).strip()
                continue
            narrative.append(line)
        details = " ".join(narrative)
        if len(details) > _DETAILS_MAX_CHARS:
            # Break on a word boundary; "..." rather than an ellipsis glyph, for
            # the same reason the tick box is drawn rather than typed.
            details = details[:_DETAILS_MAX_CHARS].rsplit(" ", 1)[0] + "..."
        return reported_by, details

    @api.model
    def _signoff_row(self, request, priority_labels, today):
        opened_on = self._opened_on(request)
        scheduled_on = (
            fields.Datetime.context_timestamp(request, request.schedule_date).date()
            if request.schedule_date
            else False
        )
        ticket_ref, subject = self._split_ticket_name(request)
        reported_by, details = self._describe(request)
        # The automation builds the title out of the description, truncated, so
        # on short tickets the details line just repeats the headline word for
        # word. Print it only when it actually says more — which on a 230-ticket
        # Engineering run is the difference between a sheet and a booklet.
        # MR-1501 is the case that earns it: its title stops mid-sentence at
        # "the plastic" and the full text names the GMP requirement behind it.
        show_details = bool(details) and not subject.endswith(details)
        # x_store_location is stamped from company_id by the region automation,
        # but it is blank on pre-automation tickets — fall back to the company
        # itself so the Location column is never empty.
        location = request.x_store_location or request.company_id.name or ""
        # The automation builds the title as "<store> - <equipment> - <text>",
        # so the store is about to be printed twice on the same row. Drop it
        # from the subject only on an exact match against the Location cell,
        # which leaves oddly-named tickets alone.
        if location and subject.startswith("%s - " % location):
            subject = subject[len(location) + 3 :].strip()
        return {
            "id": request.id,
            "ticket_ref": ticket_ref,
            "subject": subject,
            "reported_by": reported_by,
            "details": details,
            "show_details": show_details,
            # Drives both the second row and the rowspan on the Done and
            # Manager sign-off cells. Without it a ticket with nothing extra to
            # say would print an empty second line, and the rowspan would run
            # past the end of its own ticket.
            "has_second_line": show_details or bool(reported_by),
            "name": request.name or "",
            "location": location,
            "stage": request.stage_id.name or "",
            "priority": priority_labels.get(request.priority, ""),
            "is_high_priority": request.priority == "3",
            "blocked": request.kanban_state == "blocked",
            "age_days": (today - opened_on).days if opened_on else 0,
            "opened_on": format_date(self.env, opened_on) if opened_on else "",
            "scheduled_on": format_date(self.env, scheduled_on) if scheduled_on else "",
            # Overdue means the team committed to a date and that date has
            # passed. An unscheduled ticket is not late — there is nothing to be
            # late against — it shows as a blank Scheduled cell instead.
            "overdue": bool(scheduled_on and scheduled_on < today),
        }
