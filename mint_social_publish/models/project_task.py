import base64
import json
import logging
import re
import struct

import requests

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.tools import html2plaintext

_logger = logging.getLogger(__name__)

PARAM_BASE = "mint_social_publish.api_base"
PARAM_KEY = "mint_social_publish.api_key"
PARAM_PROJECT = "mint_social_publish.project_id"
PARAM_STAGE = "mint_social_publish.scheduled_stage_id"
PARAM_MAXRETRY = "mint_social_publish.max_retries"

DEFAULT_BASE = "https://api.upload-post.com"
APPROVER_GROUP = "mint_social_publish.group_social_approver"
# Fields/states that only an approver (or the system) may set — server-side gate.
APPROVAL_FIELDS = {"x_social_approved", "x_social_approved_by", "x_social_approved_at"}
APPROVAL_STATES = {"approved", "sent"}
# Sweep cap per cron run so one stuck profile can't starve the rest.
SWEEP_LIMIT = 50
HTTP_TIMEOUT = 120

# Per-platform media ceilings, used to warn the scheduler BEFORE a card is
# queued so an over-limit asset gets trimmed/compressed instead of failing at
# posts.agency. Values are the platforms' hard publish maxima (not the softer
# "best-practice" lengths) so we only block uploads that would actually be
# rejected. Duration is keyed by post type where a platform differs (story vs
# reel vs feed). Verified 2026-06 — review periodically; platforms move these.
#   max_mb        : max file size in megabytes
#   duration_s    : {post_type: max seconds}; "default" applies to auto/feed.
PLATFORM_LIMITS = {
    "instagram": {"max_mb": 1024, "duration_s": {"story": 60, "reel": 900, "default": 3600}},
    "facebook": {"max_mb": 10240, "duration_s": {"story": 60, "reel": 90, "default": 14400}},
    "tiktok": {"max_mb": 4096, "duration_s": {"default": 600}},
    "youtube": {"max_mb": 262144, "duration_s": {"default": 43200}},
    "x": {"max_mb": 512, "duration_s": {"default": 140}},
    "linkedin": {"max_mb": 5120, "duration_s": {"default": 600}},
    "pinterest": {"max_mb": 2048, "duration_s": {"default": 900}},
    "threads": {"max_mb": 1024, "duration_s": {"default": 300}},
    "bluesky": {"max_mb": 100, "duration_s": {"default": 180}},
}


def _mp4_duration_seconds(raw):
    """Best-effort video duration (float seconds) from MP4/MOV bytes.

    Walks the ISO-BMFF box tree to moov > mvhd and reads duration/timescale.
    Returns None when it can't be determined (non-MP4 container, fragmented
    file with a zero mvhd duration, truncated bytes) — callers MUST treat None
    as "unknown, don't block on duration". No external binary (ffmpeg) needed.
    """
    if not raw or len(raw) < 16:
        return None

    def _iter_boxes(start, end):
        i = start
        while i + 8 <= end:
            size = struct.unpack(">I", raw[i:i + 4])[0]
            typ = raw[i + 4:i + 8]
            header = 8
            if size == 1:  # 64-bit largesize
                if i + 16 > end:
                    break
                size = struct.unpack(">Q", raw[i + 8:i + 16])[0]
                header = 16
            elif size == 0:  # box extends to end of file
                size = end - i
            if size < header or i + size > end:
                break
            yield typ, i + header, i + size
            i += size

    try:
        for typ, s, e in _iter_boxes(0, len(raw)):
            if typ != b"moov":
                continue
            for t2, s2, _e2 in _iter_boxes(s, e):
                if t2 != b"mvhd":
                    continue
                version = raw[s2]
                if version == 1:
                    timescale = struct.unpack(">I", raw[s2 + 20:s2 + 24])[0]
                    duration = struct.unpack(">Q", raw[s2 + 24:s2 + 32])[0]
                else:
                    timescale = struct.unpack(">I", raw[s2 + 12:s2 + 16])[0]
                    duration = struct.unpack(">I", raw[s2 + 16:s2 + 20])[0]
                if timescale and duration > 0:
                    return duration / timescale
                return None
    except (struct.error, IndexError):
        return None
    return None


def _fmt_seconds(secs):
    """Human mm:ss for guard messages."""
    secs = int(round(secs))
    return "%d:%02d" % (secs // 60, secs % 60)


class ProjectTask(models.Model):
    _inherit = "project.task"

    # --- intake fields (schedulers: Ashton / Sage fill these) ---
    x_social_profile_id = fields.Many2one(
        "mint.social.profile",
        string="posts.agency Account",
        domain=[("is_connected", "=", True)],
        help="Connected posts.agency account to publish to. Shows only accounts "
        "with a platform connected; refresh in Settings > posts.agency Publishing.",
    )
    x_social_platforms = fields.Char(
        string="Social Platforms",
        default="instagram",
        help="Comma-separated platforms: instagram, tiktok, youtube, facebook, "
        "linkedin, pinterest, threads, x.",
    )
    x_social_caption = fields.Text(
        string="Post Caption",
        help="Caption/description sent to the platforms. Falls back to the task "
        "description if left empty.",
    )
    x_social_asset = fields.Binary(
        string="Final Asset",
        attachment=True,
        help="The image or video to post. Required before a post can be submitted.",
    )
    x_social_asset_filename = fields.Char(string="Asset Filename")

    # --- workflow / approval state (publisher + approvers) ---
    x_social_publish_state = fields.Selection(
        [
            ("draft", "Draft"),
            ("pending_approval", "Pending Approval"),
            ("approved", "Approved"),
            ("sent", "Queued at posts.agency"),
            ("published", "Published"),
            ("publish_failed", "Publish Failed"),
            ("failed", "Failed"),
            ("rejected", "Rejected"),
        ],
        string="Publish Status",
        default="draft",
        copy=False,
    )
    x_social_youtube_privacy = fields.Selection(
        [("public", "Public"), ("unlisted", "Unlisted"), ("private", "Private")],
        string="YouTube Privacy",
        help="Privacy for YouTube posts (ignored for other platforms).",
    )
    x_social_post_type = fields.Selection(
        [
            ("auto", "Auto (Feed / Reel)"),
            ("reel", "Reel"),
            ("story", "Story"),
        ],
        string="IG/FB Post Type",
        default="auto",
        help="How Instagram/Facebook publishes this card. 'Auto' lets "
        "posts.agency decide (photo -> feed, video -> Reel); 'Reel' forces a "
        "Reel (video only); 'Story' posts a 24h Story (image or video). Sent as "
        "media_type (Instagram) / facebook_media_type (Facebook); ignored by "
        "platforms without Reels/Stories (TikTok, YouTube, X, ...).",
    )
    x_social_post_url = fields.Char(string="Live Post URL", copy=False, readonly=True)
    x_social_platform_post_id = fields.Char(string="Platform Post ID", copy=False, readonly=True)
    x_social_reconciled_at = fields.Datetime(string="Last Checked", copy=False, readonly=True)
    x_social_approved = fields.Boolean(string="Approved", copy=False, readonly=True)
    x_social_approved_by = fields.Many2one(
        "res.users", string="Approved By", copy=False, readonly=True
    )
    x_social_approved_at = fields.Datetime(string="Approved On", copy=False, readonly=True)
    x_social_rejection_reason = fields.Text(string="Rejection Reason", copy=False, readonly=True)
    x_social_job_id = fields.Char(string="posts.agency Job ID", copy=False, readonly=True)
    x_social_publish_error = fields.Text(string="Publish Error", copy=False, readonly=True)
    x_social_published_at = fields.Datetime(string="Sent On", copy=False, readonly=True)
    x_social_retry_count = fields.Integer(
        string="Publish Attempts", default=0, copy=False, readonly=True
    )

    # ------------------------------------------------------------------
    # config + permission helpers
    # ------------------------------------------------------------------
    @api.model
    def _social_param(self, key, default=None):
        return self.env["ir.config_parameter"].sudo().get_param(key, default)

    @api.model
    def _social_param_int(self, key, default=0):
        try:
            return int(self._social_param(key, default))
        except (TypeError, ValueError):
            return default

    def _social_is_approver(self):
        return self.env.user.has_group(APPROVER_GROUP)

    # ------------------------------------------------------------------
    # connection check (Settings "Test Connection" button)
    # ------------------------------------------------------------------
    @api.model
    def _social_check_credentials(self, base=None, api_key=None):
        base = (base or self._social_param(PARAM_BASE) or DEFAULT_BASE).strip().rstrip("/")
        api_key = (api_key or self._social_param(PARAM_KEY) or "").strip()
        if not api_key:
            return (False, _("No posts.agency API key is set."))
        try:
            resp = requests.get(
                "%s/api/uploadposts/users" % base,
                headers={"Authorization": "ApiKey %s" % api_key},
                timeout=30,
            )
        except Exception as exc:  # noqa: BLE001
            return (False, _("Could not reach posts.agency: %s") % exc)
        if resp.status_code in (401, 403):
            return (False, _("posts.agency rejected the API key (HTTP %s).") % resp.status_code)
        if resp.status_code != 200:
            return (False, _("posts.agency returned HTTP %s.") % resp.status_code)
        try:
            count = len(resp.json().get("profiles", []))
        except ValueError:
            count = 0
        return (True, _("Connected to posts.agency — %s profile(s) available.") % count)

    # ------------------------------------------------------------------
    # write: approval guard + Scheduled-stage submit trigger
    # ------------------------------------------------------------------
    def write(self, vals):
        # Server-side gate: only approvers (or the system) may set the approval
        # fields or move state to approved/sent. Blocks self-approval via raw
        # RPC/MCP writes by schedulers (Ashton/Sage).
        if not self.env.su:
            touches_approval = bool(APPROVAL_FIELDS & set(vals)) or (
                vals.get("x_social_publish_state") in APPROVAL_STATES
            )
            if touches_approval and not self._social_is_approver():
                raise AccessError(
                    _("Only a Social Publish Approver (Pablo or Juan) can approve or send a post.")
                )
        res = super().write(vals)
        if vals.get("stage_id"):
            target_stage = self._social_param_int(PARAM_STAGE, 0)
            target_project = self._social_param_int(PARAM_PROJECT, 0)
            if target_stage and vals["stage_id"] == target_stage:
                for task in self:
                    if target_project and task.project_id.id != target_project:
                        continue
                    task._social_submit_for_approval()
        return res

    # ------------------------------------------------------------------
    # validation
    # ------------------------------------------------------------------
    def _social_validate(self):
        self.ensure_one()
        missing = []
        profile = self.x_social_profile_id
        if not profile:
            missing.append(_("a posts.agency account"))
        elif not profile.is_connected:
            missing.append(_("an account that has a connected platform"))
        chosen = self._social_platform_list()
        if not chosen:
            missing.append(_("at least one platform"))
        elif profile and profile.is_connected:
            allowed = set(profile.platform_list())
            bad = [p for p in chosen if p not in allowed]
            if bad:
                missing.append(
                    _("only platforms connected for this account [%(ok)s]; remove: %(bad)s")
                    % {"ok": ", ".join(sorted(allowed)) or _("none"), "bad": ", ".join(bad)}
                )
        att = self._social_media_attachment()
        if not att:
            missing.append(_("a media attachment (image or video) on the task"))
        post_type = self.x_social_post_type or "auto"
        if post_type in ("reel", "story") and not ({"instagram", "facebook"} & set(chosen)):
            missing.append(
                _("Instagram or Facebook in the platforms for a Reel/Story")
            )
        if post_type == "reel" and att and (att.mimetype or "").startswith("image/"):
            missing.append(_("a video attachment for a Reel (an image can't be a Reel)"))
        if not self.date_deadline:
            missing.append(_("a scheduled date (Deadline)"))
        elif self.date_deadline <= fields.Datetime.now():
            missing.append(_("a Deadline in the future"))
        # Per-platform size/duration ceilings: catch an over-limit asset here so
        # the scheduler trims it instead of the post failing at posts.agency.
        oversize = self._social_media_limit_problems()
        if missing or oversize:
            parts = []
            if missing:
                parts.append(_("missing: %s") % "; ".join(missing))
            if oversize:
                parts.append("; ".join(oversize))
            raise UserError(
                _('Cannot submit "%(name)s" to posts.agency — %(msg)s.')
                % {"name": self.display_name, "msg": ". ".join(parts)}
            )

    def _social_platform_list(self):
        raw = (self.x_social_platforms or "").replace(";", ",")
        return [p.strip().lower() for p in raw.split(",") if p.strip()]

    # posts.agency media_type values per IG/FB post type. "auto" is omitted so
    # the service auto-detects (photo -> feed, video -> Reel) — preserving the
    # pre-Story behaviour for cards that don't opt in.
    _SOCIAL_MEDIA_TYPE = {"reel": "REELS", "story": "STORIES"}

    def _social_media_type_params(self, platforms):
        """Form fields telling posts.agency how to publish on IG/FB.

        Instagram reads ``media_type`` and Facebook ``facebook_media_type``
        (REELS | STORIES). Returns an empty list for the "auto" post type or
        when neither IG nor FB is targeted, so other platforms are untouched.
        """
        self.ensure_one()
        mtype = self._SOCIAL_MEDIA_TYPE.get(self.x_social_post_type or "auto")
        if not mtype:
            return []
        params = []
        if "instagram" in platforms:
            params.append(("media_type", mtype))
        if "facebook" in platforms:
            params.append(("facebook_media_type", mtype))
        return params

    def _social_media_attachment(self):
        self.ensure_one()
        return (
            self.env["ir.attachment"]
            .sudo()
            .search(
                [
                    ("res_model", "=", "project.task"),
                    ("res_id", "=", self.id),
                    # the Final Asset field (res_field='x_social_asset') or a file
                    # attached via chatter (res_field=False) — but NOT images
                    # embedded in the HTML description (res_field='description').
                    ("res_field", "in", [False, "x_social_asset"]),
                    "|",
                    ("mimetype", "=ilike", "image/%"),
                    ("mimetype", "=ilike", "video/%"),
                ],
                order="id desc",
                limit=1,
            )
        )

    def _social_video_duration_seconds(self, attachment):
        """Duration (float seconds) of a video attachment, or None if unknown
        (non-video, non-MP4/MOV container, or unreadable). Best-effort: never
        raises, so an unparseable file just skips the duration guard."""
        self.ensure_one()
        if not attachment or not (attachment.mimetype or "").startswith("video/"):
            return None
        try:
            return _mp4_duration_seconds(attachment.sudo().raw)
        except Exception:  # noqa: BLE001 - guard must never block the form
            _logger.warning("Could not read duration for attachment %s", attachment.id)
            return None

    def _platform_duration_limit(self, platform, post_type):
        """Max video seconds for a platform + post type, or None if uncapped/
        unknown. Falls back to the platform's 'default' (auto/feed) limit."""
        limits = (PLATFORM_LIMITS.get(platform) or {}).get("duration_s") or {}
        return limits.get(post_type) or limits.get("default")

    def _social_media_limit_problems(self):
        """List of human, actionable over-limit messages for the chosen
        platforms (empty when the asset is within every platform's limits).
        Checks file size for any media and duration for videos."""
        self.ensure_one()
        att = self._social_media_attachment()
        if not att:
            return []
        problems = []
        platforms = self._social_platform_list()
        post_type = self.x_social_post_type or "auto"
        is_video = (att.mimetype or "").startswith("video/")
        size_mb = (att.file_size or 0) / (1024.0 * 1024.0)
        duration = self._social_video_duration_seconds(att) if is_video else None
        for platform in platforms:
            limit = PLATFORM_LIMITS.get(platform)
            if not limit:
                continue
            max_mb = limit.get("max_mb")
            if max_mb and size_mb > max_mb:
                problems.append(
                    _("%(p)s allows max %(max)s MB but this file is %(have)s MB — compress it")
                    % {"p": platform, "max": max_mb, "have": round(size_mb)}
                )
            if is_video and duration:
                max_s = self._platform_duration_limit(platform, post_type)
                if max_s and duration > max_s:
                    fmt = {"story": "Story", "reel": "Reel"}.get(post_type, "video")
                    problems.append(
                        _("%(p)s %(fmt)s max length is %(max)s but this video is %(have)s — trim it")
                        % {
                            "p": platform,
                            "fmt": fmt,
                            "max": _fmt_seconds(max_s),
                            "have": _fmt_seconds(duration),
                        }
                    )
        return problems

    # ------------------------------------------------------------------
    # workflow actions
    # ------------------------------------------------------------------
    def _social_submit_for_approval(self):
        """Scheduler step (Ashton/Sage): validate + mark pending approval.
        Does NOT send — an approver must approve."""
        self.ensure_one()
        self._social_validate()
        self.write(
            {
                "x_social_publish_state": "pending_approval",
                "x_social_publish_error": False,
                "x_social_rejection_reason": False,
                "x_social_retry_count": 0,
            }
        )

    def action_social_submit(self):
        for task in self:
            task._social_submit_for_approval()
        return True

    def action_social_approve(self):
        """Approver-only (Pablo/Juan): approve and send the post."""
        if not self._social_is_approver():
            raise AccessError(
                _("Only a Social Publish Approver (Pablo or Juan) can approve a post.")
            )
        for task in self:
            task._social_validate()
            task.write(
                {
                    "x_social_approved": True,
                    "x_social_approved_by": self.env.user.id,
                    "x_social_approved_at": fields.Datetime.now(),
                    "x_social_publish_state": "approved",
                    "x_social_rejection_reason": False,
                }
            )
            task._social_do_publish()
        return True

    def action_social_reject(self):
        """Approver-only: reject a pending post (back to the scheduler)."""
        if not self._social_is_approver():
            raise AccessError(
                _("Only a Social Publish Approver (Pablo or Juan) can reject a post.")
            )
        for task in self:
            task.write(
                {
                    "x_social_publish_state": "rejected",
                    "x_social_approved": False,
                    "x_social_approved_by": False,
                    "x_social_approved_at": False,
                    "x_social_rejection_reason": task.x_social_rejection_reason
                    or _("Rejected by %s") % self.env.user.name,
                }
            )
        return True

    # ------------------------------------------------------------------
    # single-call scheduler (plain-language / agent entry point)
    # ------------------------------------------------------------------
    @api.model
    def social_schedule(self, account, when, content, platforms=None,
                        title=None, image_url=None, submit=True):
        """One call to schedule a social post — built for agents/natural language.

        Resolves the posts.agency account by name, creates a Social Media card with
        the chosen platform(s), date/time and caption, optionally attaches an image
        from a URL, and submits it for approval. Runs with the caller's permissions,
        so a scheduler (Ashton / Sage) can prepare + submit but never approve/send.

        Returns a status dict (ok, task_id, account, platforms, scheduled_at, state,
        message / error / available_accounts).
        """
        prof = self._social_resolve_account(account)
        if not prof:
            Profile = self.env["mint.social.profile"]
            return {
                "ok": False,
                "error": _("No connected posts.agency account matches '%s'.") % account,
                "available_accounts": Profile.search([("is_connected", "=", True)]).mapped("name"),
            }
        if not platforms:
            platforms = prof.connected_platforms or "instagram"
        if isinstance(platforms, (list, tuple)):
            platforms = ",".join(str(p) for p in platforms)

        dt = self._social_parse_when(when)
        if not dt:
            return {
                "ok": False,
                "error": _("Could not understand the date/time '%s'. Try e.g. '2026-12-20 09:00'.") % when,
            }

        project_id = self._social_param_int(PARAM_PROJECT, 0) or 42
        task = self.create(
            {
                "name": (title or (content or "Social Post")).strip()[:60] or "Social Post",
                "project_id": project_id,
                "date_deadline": fields.Datetime.to_string(dt),
                "x_social_profile_id": prof.id,
                "x_social_platforms": platforms,
                "x_social_caption": content or "",
            }
        )

        if image_url:
            try:
                task._social_attach_from_url(image_url)
            except Exception as exc:  # noqa: BLE001
                return {
                    "ok": True, "task_id": task.id, "account": prof.name, "state": "draft",
                    "warning": _("Card created but the image could not be fetched: %s") % exc,
                }

        if submit and task._social_media_attachment():
            try:
                task._social_submit_for_approval()
            except UserError as exc:
                return {
                    "ok": True, "task_id": task.id, "account": prof.name, "state": "draft",
                    "needs": exc.args[0] if exc.args else _("not ready"),
                }

        state = task.x_social_publish_state
        return {
            "ok": True,
            "task_id": task.id,
            "account": prof.name,
            "platforms": platforms,
            "scheduled_at": task.date_deadline and fields.Datetime.to_string(task.date_deadline),
            "state": state,
            "message": (
                _("Submitted for approval — Pablo or Juan must approve before it is sent.")
                if state == "pending_approval"
                else _("Saved as draft. Attach the final asset, then submit it for approval.")
            ),
        }

    @api.model
    def _social_resolve_account(self, account):
        """Fuzzy-match a connected posts.agency account from a plain name.
        Handles 'Mint Florida' -> 'themintflorida' (spacing/prefix differences)."""
        Profile = self.env["mint.social.profile"]
        candidates = Profile.search([("is_connected", "=", True)])
        norm = lambda s: re.sub(r"[^a-z0-9]", "", (s or "").lower())
        na = norm(account)
        if not na:
            return Profile.browse()
        # 1) normalized containment either direction
        hit = candidates.filtered(lambda p: na in norm(p.name) or norm(p.name) in na)
        if not hit:
            # 2) all word-tokens of the request appear in the handle
            toks = [t for t in re.split(r"\W+", (account or "").lower()) if t]
            hit = candidates.filtered(
                lambda p: toks and all(t in (p.name or "").lower() for t in toks)
            )
        return hit[:1]

    def _social_parse_when(self, when):
        if not when:
            return False
        if not isinstance(when, str):
            return when
        try:
            from dateutil import parser as dtp

            return dtp.parse(when, fuzzy=True)
        except Exception:  # noqa: BLE001
            return False

    def _social_attach_from_url(self, image_url):
        self.ensure_one()
        resp = requests.get(image_url, headers={"User-Agent": "mint-social/1.0"}, timeout=60)
        resp.raise_for_status()
        ctype = (resp.headers.get("content-type") or "image/jpeg").split(";")[0].strip()
        name = (image_url.split("/")[-1].split("?")[0]) or "asset"
        self.env["ir.attachment"].create(
            {
                "name": name,
                "res_model": "project.task",
                "res_id": self.id,
                "datas": base64.b64encode(resp.content).decode(),
                "mimetype": ctype or "image/jpeg",
            }
        )

    # ------------------------------------------------------------------
    # cron sweep — only ever forwards APPROVED posts
    # ------------------------------------------------------------------
    @api.model
    def _cron_social_publish(self):
        max_retries = self._social_param_int(PARAM_MAXRETRY, 3)
        domain = [
            ("x_social_approved", "=", True),
            "|",
            ("x_social_publish_state", "=", "approved"),
            "&",
            ("x_social_publish_state", "=", "failed"),
            ("x_social_retry_count", "<", max_retries),
        ]
        tasks = self.search(domain, limit=SWEEP_LIMIT)
        for task in tasks:
            try:
                task._social_do_publish()
                self.env.cr.commit()
            except Exception as exc:  # noqa: BLE001 - keep the sweep alive
                self.env.cr.rollback()
                _logger.exception("Social publish failed for task %s", task.id)
                task.write(
                    {
                        "x_social_publish_state": "failed",
                        "x_social_publish_error": str(exc)[:2000],
                        "x_social_retry_count": task.x_social_retry_count + 1,
                    }
                )
                self.env.cr.commit()
        return True

    # ------------------------------------------------------------------
    # reconciliation — did the queued post actually publish?
    # ------------------------------------------------------------------
    @api.model
    def _cron_social_reconcile(self):
        """For 'sent' (queued) posts, ask posts.agency whether the job actually
        published. Marks them 'published' (with the live URL) or 'publish_failed'
        (with the error), and DMs the scheduler on failure."""
        base = (self._social_param(PARAM_BASE) or DEFAULT_BASE).rstrip("/")
        api_key = self._social_param(PARAM_KEY)
        if not api_key:
            return True
        headers = {"Authorization": "ApiKey %s" % api_key}
        grace_minutes = self._social_param_int("mint_social_publish.reconcile_grace_min", 30)
        now = fields.Datetime.now()
        tasks = self.search(
            [("x_social_publish_state", "=", "sent"), ("x_social_job_id", "!=", False)],
            limit=SWEEP_LIMIT,
        )
        for task in tasks:
            try:
                outcome = task._social_job_outcome(base, headers)
            except Exception:  # noqa: BLE001 - keep the sweep alive
                _logger.exception("reconcile lookup failed for task %s", task.id)
                continue
            vals = {"x_social_reconciled_at": now}
            if outcome.get("done") and outcome.get("success"):
                vals.update(
                    {
                        "x_social_publish_state": "published",
                        "x_social_post_url": outcome.get("post_url") or False,
                        "x_social_platform_post_id": outcome.get("platform_post_id") or False,
                        "x_social_publish_error": False,
                    }
                )
            elif outcome.get("done") and not outcome.get("success"):
                vals.update(
                    {
                        "x_social_publish_state": "publish_failed",
                        "x_social_publish_error": (outcome.get("error") or _("Publish failed."))[:2000],
                    }
                )
            else:
                # not done yet; flag only if well past the scheduled time
                dl = task.date_deadline
                if dl and (now - dl).total_seconds() > grace_minutes * 60:
                    vals.update(
                        {
                            "x_social_publish_state": "publish_failed",
                            "x_social_publish_error": _("No publish confirmation from posts.agency after the scheduled time."),
                        }
                    )
                else:
                    task.write(vals)
                    continue
            task.write(vals)
            if vals.get("x_social_publish_state") == "publish_failed":
                task._social_notify_failure()
            self.env.cr.commit()
        return True

    def _social_job_outcome(self, base, headers):
        """Return {done, success, post_url, platform_post_id, error} for this card's
        job, from posts.agency. Prefers the per-job status endpoint, falls back to
        the account's history."""
        self.ensure_one()
        job = self.x_social_job_id
        # 1) per-job status endpoint
        try:
            r = requests.get(
                "%s/api/uploadposts/status" % base, headers=headers,
                params={"job_id": job}, timeout=30,
            )
            if r.status_code == 200:
                d = r.json()
                # shape: {"status":"completed|failed|pending","results":[{success,post_url,...}]}
                top = (d.get("status") or "").lower()
                results = d.get("results") or []
                rec = results[0] if results else {}
                if rec and "success" in rec:
                    return {
                        "done": True,
                        "success": bool(rec.get("success")),
                        "post_url": rec.get("post_url"),
                        "platform_post_id": rec.get("platform_post_id"),
                        "error": rec.get("error_message"),
                    }
                if top == "failed":
                    return {"done": True, "success": False,
                            "error": d.get("error") or _("Publish failed.")}
        except Exception:  # noqa: BLE001
            pass
        # 2) history fallback (match job_id)
        prof = self.x_social_profile_id.name
        for page in (1, 2, 3):
            r = requests.get(
                "%s/api/uploadposts/history" % base, headers=headers,
                params={"profile": prof, "page": page}, timeout=30,
            )
            if r.status_code != 200:
                break
            hist = (r.json() or {}).get("history") or []
            for h in hist:
                if h.get("job_id") == job:
                    return {
                        "done": True, "success": bool(h.get("success")),
                        "post_url": h.get("post_url"),
                        "platform_post_id": h.get("platform_post_id"),
                        "error": h.get("error_message"),
                    }
            if not hist:
                break
        return {"done": False}

    def _social_notify_failure(self):
        """DM the scheduler (task creator) that a post failed to publish.
        Uses Discuss, not task chatter (avoids the prod message_post bug)."""
        self.ensure_one()
        partner = (self.create_uid.partner_id or self.env.user.partner_id)
        if not partner:
            return
        body = _(
            "<p>⚠️ A scheduled social post did not publish.</p>"
            "<p><b>%(name)s</b> — account %(acct)s, %(plat)s, scheduled %(when)s.</p>"
            "<p>posts.agency error: %(err)s</p>"
        ) % {
            "name": self.display_name,
            "acct": self.x_social_profile_id.name or "?",
            "plat": self.x_social_platforms or "?",
            "when": self.date_deadline or "?",
            "err": (self.x_social_publish_error or "")[:300],
        }
        try:
            channel = self.env["discuss.channel"].create(
                {
                    "name": "Social Publish Alerts",
                    "channel_type": "chat",
                    "channel_member_ids": [
                        (0, 0, {"partner_id": self.env.user.partner_id.id}),
                        (0, 0, {"partner_id": partner.id}),
                    ],
                }
            )
            channel.message_post(body=body, message_type="comment", subtype_xmlid="mail.mt_comment")
        except Exception:  # noqa: BLE001 - alerting must never break the cron
            _logger.exception("failed to DM publish-failure for task %s", self.id)

    # ------------------------------------------------------------------
    # the actual posts.agency call (never sends unless approved)
    # ------------------------------------------------------------------
    def _social_do_publish(self):
        self.ensure_one()
        if not self.x_social_approved:
            raise UserError(_("This post has not been approved for sending."))
        base = (self._social_param(PARAM_BASE) or DEFAULT_BASE).rstrip("/")
        api_key = self._social_param(PARAM_KEY)
        if not api_key:
            raise UserError(
                _("The posts.agency API key is not configured (System Parameter %s).")
                % PARAM_KEY
            )
        att = self._social_media_attachment()
        if not att:
            raise UserError(_("No image/video attachment found on this task."))

        headers = {"Authorization": "ApiKey %s" % api_key}
        data = [
            ("user", (self.x_social_profile_id.name or "").strip()),
            ("title", (self.name or "Scheduled Post")[:200]),
        ]
        caption = (self.x_social_caption or self._social_description_text()).strip()
        if caption:
            data.append(("description", caption))
        if self.date_deadline:
            data.append(("scheduled_date", self._social_scheduled_iso()))
        platforms = self._social_platform_list()
        for platform in platforms:
            data.append(("platform[]", platform))
        if "youtube" in platforms and self.x_social_youtube_privacy:
            data.append(("youtubePrivacy", self.x_social_youtube_privacy))
        for key, val in self._social_media_type_params(platforms):
            data.append((key, val))

        if (att.mimetype or "").startswith("image/"):
            if not att.datas:
                raise UserError(
                    _("Attachment '%s' has no file data to upload.") % (att.name or att.id)
                )
            url = "%s/api/upload_photos" % base
            files = [
                (
                    "photos[]",
                    (att.name or "photo.jpg", base64.b64decode(att.datas), att.mimetype or "image/jpeg"),
                )
            ]
            resp = requests.post(url, headers=headers, data=data, files=files, timeout=HTTP_TIMEOUT)
        else:
            if not att.datas:
                raise UserError(
                    _("Attachment '%s' has no file data to upload.") % (att.name or att.id)
                )
            # upload the video as binary (posts.agency can't fetch a tokenized
            # Odoo URL — "Video URL is not accessible").
            mt = att.mimetype if (att.mimetype or "").startswith("video/") else "video/mp4"
            url = "%s/api/upload" % base
            files = [("video", (att.name or "video.mp4", base64.b64decode(att.datas), mt))]
            resp = requests.post(url, headers=headers, data=data, files=files, timeout=HTTP_TIMEOUT)

        self._social_handle_response(resp)

    def _social_handle_response(self, resp):
        self.ensure_one()
        try:
            payload = resp.json()
        except ValueError:
            payload = {"raw": (resp.text or "")[:2000]}

        if not (200 <= resp.status_code < 300):
            self.write(
                {
                    "x_social_publish_state": "failed",
                    "x_social_publish_error": "HTTP %s: %s"
                    % (resp.status_code, json.dumps(payload)[:1800]),
                    "x_social_retry_count": self.x_social_retry_count + 1,
                }
            )
            return

        job_id = ""
        if isinstance(payload, dict):
            job_id = payload.get("job_id") or (payload.get("data") or {}).get("job_id") or ""
        self.write(
            {
                "x_social_publish_state": "sent",
                "x_social_job_id": job_id or False,
                "x_social_publish_error": False,
                "x_social_published_at": fields.Datetime.now(),
                "x_social_retry_count": self.x_social_retry_count + 1,
            }
        )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    @api.onchange("x_social_profile_id")
    def _onchange_social_profile_id(self):
        if self.x_social_profile_id and self.x_social_profile_id.connected_platforms:
            self.x_social_platforms = self.x_social_profile_id.connected_platforms

    def _social_description_text(self):
        self.ensure_one()
        return html2plaintext(self.description or "") if self.description else ""

    def _social_scheduled_iso(self):
        return fields.Datetime.to_datetime(self.date_deadline).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _social_attachment_public_url(self, attachment):
        attachment = attachment.sudo()
        token = attachment.access_token or attachment.generate_access_token()[0]
        base_url = self.env["ir.config_parameter"].sudo().get_param("web.base.url")
        return "%s/web/content/%s?access_token=%s&download=true" % (base_url, attachment.id, token)
