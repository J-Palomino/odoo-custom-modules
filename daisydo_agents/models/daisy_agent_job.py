import base64
import json
import logging
import re

from odoo import models, fields, api

_logger = logging.getLogger(__name__)

# SDLC lane (project.task.type id) -> the tab field that lane's output belongs in.
# Project 104 "Continuous Debugging", verified live 2026-09-03.
SDLC_LANE_FIELD = {
    2048: "x_sdlc_functional_analysis",
    2049: "x_sdlc_research",
    2050: "x_sdlc_implementation_plan",
    2051: "x_sdlc_changelog",
    2052: "x_sdlc_test_plan",
    2053: "x_sdlc_qa_report",
    2054: "x_sdlc_pr_deploy",
}

# Outbound-link lint (MR-954). Discuss renders no markdown, so [label](url)
# is rewritten to "label — url"; letsgomint Odoo links must match the
# action-window form and point at a record the posting agent can read.
MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
ODOO_URL_RE = re.compile(r"https?://letsgomint\.us/odoo/[^\s<>\"'\)\],]*")
ALLOWED_ODOO_URL_RE = re.compile(r"^https://letsgomint\.us/odoo/action-(\d+)/(\d+)$")
LINK_REMOVED_MARKER = "[link removed — failed validation]"


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

    def _sanitize_agent_reply(self, text, check_user=None):
        """Lint an outgoing agent reply before it is posted (MR-954).

        Deterministic replacement for the Flowise link-gate (adding nodes
        after the agent node disables tool execution platform-wide):
        - rewrite ``[label](url)`` to ``label — url`` (Discuss renders no
          markdown anchors);
        - replace letsgomint Odoo links that are not of the form
          ``/odoo/action-<action>/<record>`` with a removal marker;
        - replace links whose action or record does not exist or is not
          readable by ``check_user`` — the check runs under that user's
          record rules, so record existence is not leaked across access
          boundaries. Kill switch: ``daisydo_agents.link_check_tier2``.
        Fail-open: on any internal error the ORIGINAL text is returned —
        linting must never block an agent reply.
        """
        original = text or ""
        try:
            removed = 0
            text = MARKDOWN_LINK_RE.sub(
                lambda m: f"{m.group(1)} — {m.group(2)}", original
            )

            tier2 = self.env["ir.config_parameter"].sudo().get_param(
                "daisydo_agents.link_check_tier2", "1"
            ) not in ("0", "false", "False")
            check_user = check_user or self.env.user

            def _check_url(match):
                nonlocal removed
                url = match.group(0)
                trailing = ""
                stripped = url
                while stripped and stripped[-1] in ".,;:!?":
                    trailing = stripped[-1] + trailing
                    stripped = stripped[:-1]
                allowed = ALLOWED_ODOO_URL_RE.match(stripped)
                if not allowed:
                    removed += 1
                    return LINK_REMOVED_MARKER
                if tier2:
                    try:
                        action = self.env["ir.actions.act_window"].sudo().browse(
                            int(allowed.group(1))
                        )
                        model = action.exists() and action.res_model
                        visible = model and self.env[model].with_user(
                            check_user
                        ).search_count([("id", "=", int(allowed.group(2)))])
                    except Exception:
                        visible = False
                    if not visible:
                        removed += 1
                        return LINK_REMOVED_MARKER
                return stripped + trailing

            text = ODOO_URL_RE.sub(_check_url, text)

            if removed:
                _logger.warning(
                    "Agent job %s: removed %s unvalidated link(s) from reply",
                    self.ids and self.ids[0], removed,
                )
                text += (
                    f"\n\n(Note: {removed} link(s) were removed because they "
                    "could not be validated. Ask me to re-check the record "
                    "and resend.)"
                )
            return text
        except Exception:
            _logger.exception(
                "Agent job %s: reply sanitizer failed — posting unsanitized",
                self.ids and self.ids[0],
            )
            return original

    def _is_superseded(self):
        """True when a newer job exists for the same agent + target (MR-954).

        A failed job that re-pends can complete on a later cron tick than a
        job enqueued after it (seen live: job 17671 posted after 17672),
        producing confusing out-of-order double replies. The newer job's
        history contains this job's message, so skipping this reply loses
        nothing. Same-second create_dates are broken by id order.
        """
        self.ensure_one()
        return bool(self.sudo().search_count([
            ("agent_id", "=", self.agent_id.id),
            ("channel_model", "=", self.channel_model),
            ("channel_id", "=", self.channel_id),
            ("state", "!=", "error"),
            "|", ("create_date", ">", self.create_date),
            "&", ("create_date", "=", self.create_date), ("id", ">", self.id),
        ]))

    @api.model
    def _recent_history_messages(self, domain, limit):
        """Newest ``limit`` mail.messages matching ``domain``, oldest-first.

        MR-954: the enqueue hooks used ``order="date asc"`` + ``limit``,
        which pins history to the OLDEST messages once a thread outgrows
        the cap — the agent then never sees recent turns.
        """
        msgs = self.env["mail.message"].search(
            domain, order="date desc, id desc", limit=limit
        )
        return msgs.sorted(key=lambda m: (m.date, m.id))

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

                # A newer message on the same channel supersedes this reply:
                # the newer job's history includes this job's message, so
                # posting both yields out-of-order double replies (MR-954 —
                # checked AFTER the slow AI call so messages that arrived
                # while the prediction ran also supersede).
                if job._is_superseded():
                    job.write({"state": "done", "response_text": "[SUPERSEDED]"})
                    self.env.cr.commit()
                    _logger.info(
                        "Job %s superseded by a newer job on %s#%s — reply skipped",
                        job.id, job.channel_model, job.channel_id,
                    )
                    continue

                if not ai_result.get("response"):
                    job.write({"state": "error", "error_message": "Empty AI response"})
                    self.env.cr.commit()
                    continue

                # Handle handoff
                if ai_result.get("should_handoff"):
                    target.with_user(agent.user_id).message_post(
                        body="Let me connect you with a team member who can help further.",
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

                # Lint links before posting (MR-954): allowlist Odoo action
                # links, verify targets as the agent user, rewrite markdown.
                reply_body = job._sanitize_agent_reply(
                    ai_result["response"], check_user=agent.user_id
                )

                # Post AI response as agent
                ai_msg = target.with_user(agent.user_id).message_post(
                    body=reply_body,
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

                # Mirror the reply into the SDLC tab for this lane.
                #
                # The SDLC gates read x_sdlc_* fields, but nothing in this
                # codebase ever wrote them - the tabs were only ever populated
                # by the /skills over XML-RPC. That deadlocked the pipeline: an
                # agent posts a perfectly good Functional Analysis to chatter,
                # the gate reads an empty tab, and the ticket holds forever
                # (observed on #109603). The agent cannot write the tab itself
                # either - its only Odoo tool is a read-only odoo_get_record.
                #
                # Best-effort: a failure here must never lose the chatter post
                # or fail the job, so it is caught and logged.
                if job.channel_model == "project.task":
                    try:
                        field = SDLC_LANE_FIELD.get(target.stage_id.id)
                        if field and field in target._fields:
                            reply = ai_result["response"] or ""
                            existing = target[field] or ""
                            # NEVER shrink an SDLC tab.
                            #
                            # The first cut of this wrote the reply
                            # unconditionally, and agents talk: a "Thanks,
                            # Devid! Could you let me know who I should reach
                            # out to..." reply overwrote #123765's real
                            # 6,392-char Test Plan with 461 chars of chat, and
                            # both #109603 and #123765 then advanced a lane on
                            # that filler. These fields are not mail.thread
                            # tracked, so the artifact was unrecoverable.
                            #
                            # Monotonic growth is the invariant: fill an empty
                            # tab, or replace with something LONGER. A real
                            # artifact can still supersede earlier filler, but
                            # no chat reply can destroy real work. Judging
                            # quality is the gate's job, not this hook's.
                            if len(reply.strip()) > len(existing.strip()):
                                target.sudo().write({field: reply})
                                _logger.info(
                                    "SDLC_TAB_WRITE task=%s stage=%s field=%s "
                                    "%s->%s chars", target.id,
                                    target.stage_id.id, field,
                                    len(existing), len(reply))
                            else:
                                _logger.info(
                                    "SDLC_TAB_SKIP task=%s field=%s: reply %s "
                                    "chars would shrink existing %s chars",
                                    target.id, field, len(reply), len(existing))
                    except Exception:
                        _logger.exception(
                            "Job %s: could not mirror the reply into the SDLC tab",
                            job.id)

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
