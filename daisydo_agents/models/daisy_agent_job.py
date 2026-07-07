import base64
import json
import logging

from odoo import models, fields, api

_logger = logging.getLogger(__name__)


class DaisyAgentJob(models.Model):
    _name = "daisy.agent.job"
    _description = "Daisy Agent AI Response Job"
    _order = "create_date ASC"

    agent_id = fields.Many2one("daisy.agent", required=True, ondelete="cascade", index=True)
    channel_model = fields.Char(required=True, help="Target model, e.g. discuss.channel or project.task")
    channel_id = fields.Integer(required=True, help="Target record ID")
    message_text = fields.Text(help="Plain text of the user's message")
    conversation_history = fields.Text(help="JSON-serialized conversation history")
    conversation_id = fields.Char(help="Daisy+ chatId for multi-turn continuity")
    context_prefix = fields.Text(help="Document context for mail.thread responses")
    session_id = fields.Char(help="Daisy+ sessionId — stable per-user memory key (partner-N or guest-N)")

    state = fields.Selection(
        [
            ("pending", "Pending"),
            ("processing", "Processing"),
            ("done", "Done"),
            ("error", "Error"),
            ("expired", "Expired"),
        ],
        default="pending",
        required=True,
        index=True,
    )
    error_message = fields.Text()
    response_text = fields.Text(help="AI response for audit trail")
    attempts = fields.Integer(default=0)
    max_attempts = fields.Integer(default=3)

    @api.model
    def _cron_process_jobs(self):
        """Called by ir.cron — process pending AI response jobs.

        Each pending row is claimed with ``SELECT ... FOR UPDATE SKIP LOCKED``
        so overlapping cron runs (every ``_enqueue_response`` fires
        ``_trigger()``) never claim the same row — a claimed job is processed
        by exactly one worker. Within a single run a job is touched at most
        once (see ``seen`` below): a job that fails and is re-pended waits for
        the next tick rather than retrying immediately and starving newer jobs.

        Note: this guarantees single *claiming* of a row, not single
        *enqueuing*. Dedup of duplicate enqueues is best-effort in
        ``_enqueue_response`` — a truly simultaneous double-enqueue can still
        create two rows (a partial unique index would be needed to prevent it).
        """
        # Reclaim jobs orphaned in 'processing'. A job is flipped to
        # 'processing' and committed (releasing its row lock) BEFORE the slow
        # Daisy+ call; if the worker then dies mid-call — e.g. a Railway
        # redeploy during a 15s+ prediction — the except-handler never runs and
        # the row is stranded. The claim SELECT below only looks at 'pending',
        # so a stranded job is never retried and the agent goes silent until a
        # human notices. attempts was already incremented at claim time, so the
        # max_attempts ceiling still bounds retries. write_date is the last
        # state flip, so a row 'processing' longer than the stale window
        # (well past any real prediction) is presumed dead and re-pended.
        stale_cutoff = fields.Datetime.subtract(fields.Datetime.now(), minutes=5)
        orphaned = self.sudo().search([
            ("state", "=", "processing"),
            ("write_date", "<", stale_cutoff),
        ])
        if orphaned:
            orphaned.write({
                "state": "pending",
                "error_message": "Reclaimed: orphaned in 'processing' (worker likely died mid-call)",
            })
            self.env.cr.commit()
            _logger.warning(
                "Reclaimed %s orphaned 'processing' agent job(s): %s",
                len(orphaned), orphaned.ids,
            )

        # ids touched this run — don't re-pick a re-pended failure. Seeded with
        # a sentinel 0 (no real job has id 0) so the array passed to ALL() is
        # never empty, which would otherwise be an untyped-array SQL error.
        seen = [0]
        for _ in range(10):
            # Atomically claim the oldest unlocked pending job not yet touched
            # this run. SKIP LOCKED makes a parallel worker skip a row another
            # worker is holding rather than block on it or double-process it.
            self.env.cr.execute(
                """
                SELECT id FROM daisy_agent_job
                WHERE state = 'pending' AND id <> ALL(%s)
                ORDER BY create_date ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
                """,
                (seen,),
            )
            row = self.env.cr.fetchone()
            if not row:
                break
            seen.append(row[0])
            job = self.sudo().browse(row[0])

            if job.attempts >= job.max_attempts:
                job.write({"state": "error", "error_message": "Max attempts exceeded"})
                self.env.cr.commit()
                continue

            try:
                # Flip to 'processing' while the row lock from the SELECT is
                # still held, then commit to durably claim it and release the
                # lock before the slow Daisy+ API call.
                job.write({"state": "processing", "attempts": job.attempts + 1})
                self.env.cr.commit()

                agent = job.agent_id
                target = self.env[job.channel_model].browse(job.channel_id)
                if not agent.exists() or not target.exists():
                    job.write({"state": "error", "error_message": "Agent or target record no longer exists"})
                    self.env.cr.commit()
                    continue

                # Show typing indicator (Discuss channels only)
                member = None
                if job.channel_model == "discuss.channel":
                    member = self.env["discuss.channel.member"].sudo().search([
                        ("channel_id", "=", target.id),
                        ("partner_id", "=", agent.partner_id.id),
                    ], limit=1)
                    if member:
                        member._notify_typing(True)
                        self.env.cr.commit()

                # Build history and call Daisy+ API
                history = json.loads(job.conversation_history) if job.conversation_history else []
                full_text = (job.context_prefix or "") + job.message_text
                ai_result = agent.get_ai_response(
                    full_text, history, job.conversation_id, session_id=job.session_id or None,
                )

                # Stop typing
                if member:
                    member._notify_typing(False)

                if not ai_result.get("response"):
                    job.write({"state": "error", "error_message": "Empty AI response"})
                    self.env.cr.commit()
                    continue

                # Label automated DM replies so the recipient knows it isn't the
                # human personally — the agent posts as the human's own user, and
                # in a 1:1 DM this is the away-autoresponder answering for them.
                # Livechat (visitor-facing) is left unlabelled. Configurable via
                # ir.config_parameter 'daisy.autoreply_prefix' (set empty to disable).
                autoreply_prefix = ""
                if job.channel_model == "discuss.channel" and getattr(target, "channel_type", False) == "chat":
                    autoreply_prefix = self.env["ir.config_parameter"].sudo().get_param(
                        "daisy.autoreply_prefix", "[Automated Reply]: "
                    )

                # Handle handoff
                if ai_result.get("should_handoff"):
                    target.with_user(agent.user_id).message_post(
                        body=f"{autoreply_prefix}Let me connect you with a team member who can help further.",
                        message_type="comment",
                        subtype_xmlid="mail.mt_comment",
                    )
                    job.write({"state": "done", "response_text": "[HANDOFF]"})
                    self.env.cr.commit()
                    continue

                # Build image attachments (e.g. MeshCentral screenshots the
                # agency captured) so they render inline in Discuss/chatter.
                attachments = []
                for idx, img in enumerate(ai_result.get("images") or []):
                    ext = (img.get("mime") or "image/png").split("/")[-1].split("+")[0]
                    if ext == "jpeg":
                        ext = "jpg"
                    try:
                        attachments.append(
                            (f"screenshot-{idx + 1}.{ext}", base64.b64decode(img["b64"]))
                        )
                    except Exception:
                        _logger.warning("Job %s: could not decode agent image", job.id)

                # Post AI response as agent
                ai_msg = target.with_user(agent.user_id).message_post(
                    body=f"{autoreply_prefix}{ai_result['response']}",
                    message_type="comment",
                    subtype_xmlid="mail.mt_comment",
                    attachments=attachments or None,
                )
                ai_msg.sudo().write({
                    "daisy_ai_generated": True,
                    "daisy_ai_confidence": ai_result.get("confidence", 0),
                    "daisy_intent": ai_result.get("intent", ""),
                    "daisy_conversation_id": ai_result.get("conversation_id", ""),
                })

                job.write({
                    "state": "done",
                    "response_text": ai_result["response"][:5000],
                })
                self.env.cr.commit()

            except Exception as e:
                self.env.cr.rollback()
                # Re-browse after rollback to get a fresh record
                job = self.sudo().browse(job.id)
                new_state = "pending" if job.attempts < job.max_attempts else "error"
                job.write({
                    "state": new_state,
                    "error_message": str(e)[:2000],
                })
                self.env.cr.commit()
                _logger.exception("Job %s failed (attempt %s): %s", job.id, job.attempts, e)

    @api.model
    def _cron_cleanup_old_jobs(self):
        """Remove completed/error jobs older than 7 days."""
        cutoff = fields.Datetime.subtract(fields.Datetime.now(), days=7)
        old_jobs = self.sudo().search([
            ("state", "in", ("done", "error", "expired")),
            ("create_date", "<", cutoff),
        ])
        count = len(old_jobs)
        if count:
            old_jobs.unlink()
            _logger.info("Cleaned up %s old agent jobs", count)

    @api.model
    def _cron_log_timesheets(self):
        """Tier A: log each completed agent execution as a nominal timesheet line
        (``account.analytic.line``) on the agent's email project, attributed to the
        agent's ``hr.employee``.

        - Idempotent: processes only jobs newer than the ``daisy.timesheet.last_job_id``
          watermark, then advances it (re-runs never double-count).
        - Nominal duration (``daisy.timesheet.nominal_hours``, default 0.1h) — the real
          per-execution timing lives on daisy.plus, not in Odoo (job create->write is DB
          latency). Swap in daisy.plus /executions timing for billing accuracy (Tier B).
        - Soft-gated: no-op unless ``hr_timesheet`` is installed (keeps the module
          installable without force-pulling hr_timesheet on environments that lack it).
        - Coverage: skips agents whose backing user has no ``hr.employee``.
        """
        IMM = self.env["ir.module.module"].sudo()
        if not IMM.search_count([("name", "=", "hr_timesheet"), ("state", "=", "installed")]):
            return 0
        ICP = self.env["ir.config_parameter"].sudo()
        watermark = int(ICP.get_param("daisy.timesheet.last_job_id", "0") or "0")
        nominal = float(ICP.get_param("daisy.timesheet.nominal_hours", "0.1") or "0.1")
        jobs = self.sudo().search(
            [("id", ">", watermark), ("state", "=", "done")], order="id asc", limit=500)
        if not jobs:
            return 0
        Emp = self.env["hr.employee"].sudo()
        Proj = self.env["project.project"].sudo()
        AAL = self.env["account.analytic.line"].sudo()
        max_id = watermark
        made = 0
        for job in jobs:
            max_id = max(max_id, job.id)
            agent = job.agent_id
            if not agent or not agent.user_id:
                continue
            emp = Emp.search([("user_id", "=", agent.user_id.id)], limit=1)
            if not emp:
                continue  # Tier A coverage: only agents with an hr.employee
            proj = Proj.search(
                [("daisy_agent_id", "=", agent.id), ("allow_timesheets", "=", True)], limit=1)
            if not proj:
                continue
            day = (job.create_date or fields.Datetime.now()).date()
            AAL.create({
                "name": "[daisy:%s] %s execution" % (job.id, agent.name or agent.code or agent.id),
                "project_id": proj.id,
                "employee_id": emp.id,
                "unit_amount": nominal,
                "date": day,
            })
            made += 1
        ICP.set_param("daisy.timesheet.last_job_id", str(max_id))
        if made:
            _logger.info(
                "Daisy timesheet cron: logged %s execution(s); watermark -> %s", made, max_id)
        return made
