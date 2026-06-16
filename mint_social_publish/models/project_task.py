import base64
import json
import logging

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
                    ("res_field", "=", False),
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
