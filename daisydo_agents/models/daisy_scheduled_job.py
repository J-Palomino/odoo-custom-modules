# -*- coding: utf-8 -*-
"""
One-time, future-dated jobs for daisy agents.

An agent (e.g. HUGO) can schedule itself to act at a specific time by creating
a daisy.scheduled.job — via the Odoo MCP create_record action — with a run_at
and a free-text instruction. A cron fires each due job exactly once by calling
the target agent's chatflow with the instruction (agent.get_ai_response), so
the agent then executes it with its own tools (e.g. sending a text). The record
is the audit row.

This is one-shot scheduling (not a recurring cron): state moves
scheduled -> done/failed and is never re-fired.
"""
import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

# Cap per cron tick so a backlog can't run away inside one transaction.
RUN_BATCH = 50


class DaisyScheduledJob(models.Model):
    _name = "daisy.scheduled.job"
    _description = "Daisy Agent Scheduled Job (one-time)"
    _order = "run_at asc"

    name = fields.Char(required=True, help="Short label for this scheduled action.")
    agent_id = fields.Many2one(
        "daisy.agent",
        string="Agent",
        required=True,
        ondelete="cascade",
        index=True,
        help="The agent that will be invoked with the instruction at run time.",
    )
    run_at = fields.Datetime(
        string="Run At (UTC)",
        required=True,
        index=True,
        help="When to fire the instruction. Stored in UTC like all Odoo datetimes.",
    )
    instruction = fields.Text(
        required=True,
        help="The message sent to the agent's chatflow when the job fires. "
             "The agent executes it with its own tools.",
    )
    session_id = fields.Char(
        help="Optional daisy session id for conversation memory. Defaults to "
             "'sched-<id>' when blank.",
    )
    state = fields.Selection(
        [
            ("scheduled", "Scheduled"),
            ("done", "Done"),
            ("failed", "Failed"),
            ("canceled", "Canceled"),
        ],
        default="scheduled",
        required=True,
        index=True,
        copy=False,
    )
    result = fields.Text(readonly=True, copy=False)
    error_message = fields.Char(readonly=True, copy=False)
    executed_at = fields.Datetime(readonly=True, copy=False)
    requested_by_id = fields.Many2one(
        "res.users",
        string="Requested By",
        default=lambda self: self.env.user,
        readonly=True,
    )

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
    def _run(self):
        """Fire one job through its agent. Isolated so one failure never
        rolls back sibling jobs in the same cron tick."""
        self.ensure_one()
        if self.state != "scheduled":
            return
        try:
            with self.env.cr.savepoint():
                resp = self.agent_id.get_ai_response(
                    self.instruction,
                    session_id=self.session_id or ("sched-%s" % self.id),
                )
                self.write({
                    "state": "done",
                    "executed_at": fields.Datetime.now(),
                    "result": (str(resp) if resp is not None else "")[:8000],
                })
        except Exception as e:  # noqa: BLE001 — log + mark failed, keep going
            _logger.exception("daisy.scheduled.job %s failed", self.id)
            self.write({
                "state": "failed",
                "executed_at": fields.Datetime.now(),
                "error_message": str(e)[:500],
            })

    @api.model
    def _cron_run_due_jobs(self):
        due = self.search(
            [("state", "=", "scheduled"), ("run_at", "<=", fields.Datetime.now())],
            limit=RUN_BATCH,
            order="run_at asc",
        )
        for job in due:
            job._run()
        return True

    # ------------------------------------------------------------------
    # UI actions
    # ------------------------------------------------------------------
    def action_run_now(self):
        for job in self:
            job._run()
        return True

    def action_cancel(self):
        self.filtered(lambda j: j.state == "scheduled").write({"state": "canceled"})
        return True
