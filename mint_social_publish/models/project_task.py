import base64
import json
import logging
import re

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
            ("sent", "Sent to posts.agency"),
            ("failed", "Failed"),
            ("rejected", "Rejected"),
        ],
        string="Publish Status",
        default="draft",
        copy=False,
    )
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
        if not self._social_media_attachment():
            missing.append(_("a media attachment (image or video) on the task"))
        if not self.date_deadline:
            missing.append(_("a scheduled date (Deadline)"))
        elif self.date_deadline <= fields.Datetime.now():
            missing.append(_("a Deadline in the future"))
        if missing:
            raise UserError(
                _('Cannot submit "%(name)s" to posts.agency — missing: %(items)s.')
                % {"name": self.display_name, "items": "; ".join(missing)}
            )

    def _social_platform_list(self):
        raw = (self.x_social_platforms or "").replace(";", ",")
        return [p.strip().lower() for p in raw.split(",") if p.strip()]

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
        for platform in self._social_platform_list():
            data.append(("platform[]", platform))

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
            url = "%s/api/upload" % base
            data.append(("video", self._social_attachment_public_url(att)))
            resp = requests.post(url, headers=headers, data=data, timeout=HTTP_TIMEOUT)

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
