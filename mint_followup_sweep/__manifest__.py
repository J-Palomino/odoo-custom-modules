{
    "name": "Mint Follow-up Sweep",
    "version": "19.0.1.0.0",
    "summary": "Daily reconciliation of follow-up calendar events against mail.activity reminders",
    "description": """
Reconciles the two halves of a scheduled follow-up: the calendar.event that
nags, and the mail.activity that tracks the work. Either one existing without
the other is a follow-up that will be silently dropped.

Reports both directions into Discuss (DevOps Alerts):

  * events with no activity      - the calendar nags but nothing is tracked
                                   (or the work is done and the reminder is stale)
  * activities with no event     - the tracker exists but nothing will surface it
                                   on the day. This direction is what hid a
                                   two-day dead calendar leg.
  * follow-up events with no task id in the description - unlinkable
  * activities due today / newly slipped yesterday

Why this module exists at all: the sweep ran for months as a bare ir.cron +
ir.actions.server pair created directly in the database, owned by no module.
That made it invisible to code review and meant a database restore from
anywhere else silently lost the only mechanism reconciling follow-ups. Moving
it into a module fixes both.

Deliberately narrow: 554 of 614 open activities were already overdue when the
sweep was written, so listing "overdue" daily is a firehose nobody reads. Only
actionable slices are listed; the backlog is a single counter so it stays
visible without being noise. The sweep posts nothing when every section is
empty.

The cron ships INACTIVE. Installing this module must not double-post alongside
the pre-existing database cron; see the cutover note in data/ir_cron.xml.
    """,
    "category": "Tools",
    "author": "Mint Cannabis",
    "website": "https://letsgomint.us",
    "license": "LGPL-3",
    "depends": ["mail", "calendar", "project"],
    "data": [
        "data/ir_cron.xml",
    ],
    "installable": True,
    "application": False,
    "auto_install": False,
}
