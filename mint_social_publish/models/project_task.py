import base64
import json
import logging

import requests

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import html2plaintext

_logger = logging.getLogger(__name__)

PARAM_BASE = "mint_social_publish.api_base"
PARAM_KEY = "mint_social_publish.api_key"
PARAM_PROJECT = "mint_social_publish.project_id"
PARAM_STAGE = "mint_social_publish.scheduled_stage_id"
PARAM_MAXRETRY = "mint_social_publish.max_retries"

DEFAULT_BASE = "https://api.upload-post.com"
# Sweep cap per cron run so one stuck profile can't starve the rest.
SWEEP_LIMIT = 50
HTTP_TIMEOUT = 120


class ProjectTask(models.Model):
    _inherit = "project.task"

    # --- intake fields (user fills these on the card) ---
    x_social_profile_id = fields.Many2one(
        "mint.social.profile",
        string="posts.agency Account",
        domain=[("is_connected", "=", True)],
        help="Connected posts.agency account to publish to. The list shows only "
        "accounts that have a platform connected; refresh it in "
        "Settings > posts.agency Publishing.",
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

    # --- result tracking (written by the publisher) ---
    x_social_publish_state = fields.Selection(
        [
            ("draft", "Draft"),
            ("pending", "Pending"),
            ("sent", "Sent to posts.agency"),
            ("failed", "Failed"),
        ],
        string="Publish Status",
        default="draft",
        copy=False,
    )
    x_social_job_id = fields.Char(string="posts.agency Job ID", copy=False, readonly=True)
    x_social_publish_error = fields.Text(string="Publish Error", copy=False, readonly=True)
    x_social_published_at = fields.Datetime(string="Forwarded At", copy=False, readonly=True)
    x_social_retry_count = fields.Integer(
        string="Publish Attempts", default=0, copy=False, readonly=True
    )

    # ------------------------------------------------------------------
    # config helpers
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

    # ------------------------------------------------------------------
    # connection check (used by the Settings "Test Connection" button)
    # ------------------------------------------------------------------
    @api.model
    def _social_check_credentials(self, base=None, api_key=None):
        """Ping posts.agency with the given (or configured) credentials.

        Returns a (ok: bool, message: str) tuple. Never raises; never returns
        account identifiers — only a yes/no and a profile count.
        """
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
        if resp.status_code == 401 or resp.status_code == 403:
            return (False, _("posts.agency rejected the API key (HTTP %s).") % resp.status_code)
        if resp.status_code != 200:
            return (False, _("posts.agency returned HTTP %s.") % resp.status_code)
        try:
            count = len(resp.json().get("profiles", []))
        except ValueError:
            count = 0
        return (True, _("Connected to posts.agency — %s profile(s) available.") % count)

    @api.onchange("x_social_profile_id")
    def _onchange_social_profile_id(self):
        """Default the platforms to whatever the chosen account has connected."""
        if self.x_social_profile_id and self.x_social_profile_id.connected_platforms:
            self.x_social_platforms = self.x_social_profile_id.connected_platforms

    # ------------------------------------------------------------------
    # trigger: entering the "Scheduled" stage enqueues the card
    # ------------------------------------------------------------------
    def write(self, vals):
        res = super().write(vals)
        if vals.get("stage_id"):
            target_stage = self._social_param_int(PARAM_STAGE, 0)
            target_project = self._social_param_int(PARAM_PROJECT, 0)
            if target_stage and vals["stage_id"] == target_stage:
                for task in self:
                    if target_project and task.project_id.id != target_project:
                        continue
                    task._social_enqueue()
        return res

    def _social_enqueue(self):
        """Validate and mark the card pending. Raises UserError (blocking the
        stage move) when the card is not publish-ready."""
        self.ensure_one()
        self._social_validate()
        # nested write carries no stage_id, so the trigger above won't recurse
        self.write(
            {
                "x_social_publish_state": "pending",
                "x_social_publish_error": False,
                "x_social_retry_count": 0,
            }
        )

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
            # block platforms the account doesn't actually have linked — this is
            # the silent-loss case (scheduled posts to an unlinked platform get a
            # 202 but never publish).
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
                _('Cannot publish "%(name)s" to posts.agency — missing: %(items)s.')
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
                    # res_field=False excludes images embedded in the HTML
                    # description (those carry res_field='description').
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
    # cron sweep + manual button
    # ------------------------------------------------------------------
    @api.model
    def _cron_social_publish(self):
        max_retries = self._social_param_int(PARAM_MAXRETRY, 3)
        domain = [
            "|",
            ("x_social_publish_state", "=", "pending"),
            "&",
            ("x_social_publish_state", "=", "failed"),
            ("x_social_retry_count", "<", max_retries),
        ]
        tasks = self.search(domain, limit=SWEEP_LIMIT)
        for task in tasks:
            try:
                task._social_do_publish()
                # commit per card so one later failure can't roll back successes
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

    def action_social_publish_now(self):
        for task in self:
            task._social_validate()
            task._social_do_publish()
        return True

    # ------------------------------------------------------------------
    # the actual posts.agency call
    # ------------------------------------------------------------------
    def _social_do_publish(self):
        self.ensure_one()
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
        # multipart form: lists are sent as repeated keys (platform[])
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
                    (
                        att.name or "photo.jpg",
                        base64.b64decode(att.datas),
                        att.mimetype or "image/jpeg",
                    ),
                )
            ]
            resp = requests.post(url, headers=headers, data=data, files=files, timeout=HTTP_TIMEOUT)
        else:
            # Videos: the service fetches a public URL (mirrors the working
            # posts.agency path); avoids streaming large bytes through Odoo.
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
    # small formatting helpers
    # ------------------------------------------------------------------
    def _social_description_text(self):
        self.ensure_one()
        return html2plaintext(self.description or "") if self.description else ""

    def _social_scheduled_iso(self):
        # date_deadline is stored naive-UTC; emit ISO-8601 with explicit Z.
        return fields.Datetime.to_datetime(self.date_deadline).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

    def _social_attachment_public_url(self, attachment):
        attachment = attachment.sudo()
        token = attachment.access_token or attachment.generate_access_token()[0]
        base_url = self.env["ir.config_parameter"].sudo().get_param("web.base.url")
        return "%s/web/content/%s?access_token=%s&download=true" % (
            base_url,
            attachment.id,
            token,
        )
