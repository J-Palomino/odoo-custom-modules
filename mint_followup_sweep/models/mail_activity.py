# -*- coding: utf-8 -*-
"""Follow-up reconciliation sweep.

Ported from the ir.actions.server that ran this as inline code in the database.
The port itself is the point: as a DB-only record it was invisible to code
review and lost on any restore from elsewhere.

Two constraints the original fought no longer apply, because this is real Python
rather than ir.cron's safe_eval sandbox:

  * `re` was unavailable, so task ids were pulled out with plain string scans.
    The scans are kept as-is anyway - they are what has been running in
    production, and a live alerting cron is a bad place to swap a tested
    extractor for an equivalent one.
  * a bare `return` was a SyntaxError at module level, so the "nothing to say"
    case had to be a guard flag. That is now an ordinary early return.

One behavioural addition: the original only looked events -> activities, so an
activity with no calendar event was invisible. That is the direction that hid a
two-day dead calendar leg, and it is now reported.
"""

from datetime import timedelta

from odoo import api, fields, models

CHANNEL_ID = 72          # Discuss: DevOps Alerts
LOOKAHEAD_DAYS = 1       # also flag reminders firing tomorrow
PHOENIX_UTC_OFFSET = 7   # Phoenix is UTC-7 year round (no DST)


def _digits_after(text, marker):
    """First run of digits following `marker`, or None."""
    i = text.find(marker)
    if i == -1:
        return None
    j = i + len(marker)
    out = ''
    while j < len(text) and text[j].isdigit():
        out += text[j]
        j += 1
    return int(out) if out else None


def _task_id(desc):
    """Task id from either URL shape the wrap-up skill has emitted.

    The bare `id=` fallback is deliberately fussy. A follow-up event often cites
    more than one record - e.g. "Ticket: /web#id=1440&model=maintenance.request"
    followed by "Blocked task: /web#id=94568&model=project.task" - and taking the
    first `id=` in the text picked up the maintenance.request, then rendered it as
    a project.task link and reported the real task as having no event at all. So
    an `id=` is only accepted when its own query string says it is a project.task.
    Requiring a URL delimiter in front also stops `nodeid=` / `sessionid=` from
    matching.
    """
    if not desc:
        return None
    tid = _digits_after(desc, '/task/')
    if tid:
        return tid
    pos = 0
    while True:
        i = desc.find('id=', pos)
        if i == -1:
            return None
        pos = i + 3
        if i > 0 and desc[i - 1] not in '#&?':
            continue
        j, out = pos, ''
        while j < len(desc) and desc[j].isdigit():
            out += desc[j]
            j += 1
        if out and 'model=project.task' in desc[j:j + 60]:
            return int(out)


def _task_link(tid):
    return '<a href="https://letsgomint.us/odoo/project/104/task/%s">#%s</a>' % (tid, tid)


class MailActivity(models.Model):
    _inherit = 'mail.activity'

    @api.model
    def _cron_followup_reconciliation_sweep(self):
        """Reconcile follow-up calendar events against their tracking activities.

        Posts a digest to Discuss channel CHANNEL_ID, or stays silent when every
        section is empty - a daily message that is usually empty trains the
        reader to ignore it.
        """
        # The server clock is UTC; follow-ups are scheduled in Phoenix terms.
        today = (fields.Datetime.now() - timedelta(hours=PHOENIX_UTC_OFFSET)).date()
        yesterday = today - timedelta(days=1)
        horizon = today + timedelta(days=LOOKAHEAD_DAYS)

        Activity = self.env['mail.activity'].sudo()
        Event = self.env['calendar.event'].sudo()

        # --- 1. reminders about to fire with nothing tracking them ------------
        events = Event.search([
            ('name', 'ilike', 'Follow up'),
            ('start', '>=', str(today)),
            ('start', '<=', str(horizon) + ' 23:59:59'),
        ])
        stale, unlinked, event_task_ids = [], [], set()
        for ev in events:
            tid = _task_id(ev.description or '')
            if not tid:
                unlinked.append(ev)
                continue
            event_task_ids.add(tid)
            has_open = Activity.search_count([
                ('res_model', '=', 'project.task'), ('res_id', '=', tid),
            ])
            if not has_open:
                stale.append((ev, tid))

        # --- 2. the actionable activity slices -------------------------------
        due_today = Activity.search([('date_deadline', '=', today)], order='id')
        newly_over = Activity.search([('date_deadline', '=', yesterday)], order='id')
        total_open = Activity.search_count([])
        total_over = Activity.search_count([('date_deadline', '<', today)])

        # --- 3. the inverse blind spot: activities with no calendar event -----
        # The original sweep only walked events -> activities, so an activity due
        # imminently with no event to surface it was invisible - which is exactly
        # how a dead calendar leg went unnoticed for two days. A task already
        # carrying an upcoming follow-up event is fine; anything else in the
        # window has no dated entry that will fire.
        no_event = []
        for act in Activity.search([
            ('res_model', '=', 'project.task'),
            ('date_deadline', '>=', today),
            ('date_deadline', '<=', horizon),
        ], order='date_deadline, id'):
            if act.res_id not in event_task_ids:
                no_event.append(act)

        if not (stale or unlinked or due_today or newly_over or no_event):
            return

        lines = ['<p><b>Follow-up reconciliation &mdash; %s</b></p>' % today]

        if stale:
            lines.append('<p>&#128308; <b>%d reminder(s) firing with no open activity</b> &mdash; '
                         'the calendar will nag but nothing is tracked, or the work is '
                         'already done and the reminder is stale:</p><ul>' % len(stale))
            for ev, tid in stale:
                lines.append('<li>%s &mdash; task %s &mdash; <i>%s</i></li>'
                             % (ev.start.strftime('%Y-%m-%d %H:%M UTC'), _task_link(tid),
                                (ev.name or '')[:90]))
            lines.append('</ul>')

        if no_event:
            lines.append('<p>&#128309; <b>%d activity(ies) due with no calendar event</b> &mdash; '
                         'tracked, but nothing dated will surface them on the day. A run of '
                         'these means the calendar leg itself is broken, not one reminder:</p><ul>'
                         % len(no_event))
            for act in no_event[:15]:
                lines.append('<li>%s &mdash; %s &mdash; <i>%s</i></li>'
                             % (act.date_deadline, _task_link(act.res_id),
                                (act.summary or act.activity_type_id.name or 'To-Do')[:80]))
            if len(no_event) > 15:
                lines.append('<li>&hellip; and %d more</li>' % (len(no_event) - 15))
            lines.append('</ul>')

        if unlinked:
            lines.append('<p>&#9888;&#65039; <b>%d follow-up event(s) with no task id</b> in the '
                         'description &mdash; unlinkable, so nothing can reconcile them:</p><ul>'
                         % len(unlinked))
            for ev in unlinked:
                lines.append('<li>%s &mdash; <i>%s</i></li>'
                             % (ev.start.strftime('%Y-%m-%d'), (ev.name or '')[:90]))
            lines.append('</ul>')

        if due_today:
            lines.append('<p>&#128204; <b>%d activity(ies) due today</b>:</p><ul>' % len(due_today))
            for a in due_today[:15]:
                who = a.user_id.name or '?'
                lines.append('<li>%s &mdash; %s <span style="color:#888">(%s)</span></li>'
                             % ((a.summary or a.activity_type_id.name or 'To-Do')[:80],
                                _task_link(a.res_id) if a.res_model == 'project.task' else a.res_model,
                                who))
            if len(due_today) > 15:
                lines.append('<li>&hellip; and %d more</li>' % (len(due_today) - 15))
            lines.append('</ul>')

        if newly_over:
            lines.append('<p>&#128992; <b>%d slipped yesterday</b> (newly overdue):</p><ul>'
                         % len(newly_over))
            for a in newly_over[:10]:
                lines.append('<li>%s &mdash; %s</li>'
                             % ((a.summary or 'To-Do')[:80],
                                _task_link(a.res_id) if a.res_model == 'project.task' else a.res_model))
            lines.append('</ul>')

        lines.append('<p style="color:#888"><i>Backlog: %d open activities, %d overdue. '
                     'Reported by ir.cron "Follow-up reconciliation sweep".</i></p>'
                     % (total_open, total_over))

        body = ''.join(lines)
        channel = self.env['discuss.channel'].sudo().browse(CHANNEL_ID)
        if channel.exists():
            # message_post escapes a plain str, so the HTML would render as
            # literal tags. Wrapping through a new mail.message keeps it markup.
            channel.message_post(
                body=self.env['mail.message'].new({'body': body}).body,
                message_type='comment',
                subtype_xmlid='mail.mt_comment',
            )
