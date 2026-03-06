import base64
import logging
import re

from odoo import models, fields, api
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class DaisyAgentFeed(models.Model):
    _name = "daisy.agent.feed"
    _description = "Agent Camera Feed"
    _order = "name"

    # --- Identity ---
    name = fields.Char(required=True, help="e.g. Tempe Front Door")
    agent_id = fields.Many2one(
        "daisy.agent", required=True, ondelete="cascade", index=True,
    )
    company_id = fields.Many2one(
        "res.company", string="Store Location",
        help="Store this camera is physically located at",
    )

    # --- Feed Configuration ---
    feed_type = fields.Selection(
        [
            ("rtsp_in", "Camera (RTSP In)"),
            ("rtsp_out", "Broadcast (RTSP Out)"),
        ],
        required=True,
        default="rtsp_in",
    )
    rtsp_url = fields.Char(
        string="RTSP URL",
        help="Full RTSP URL for camera source, e.g. rtsp://192.168.1.100:554/stream1",
    )
    feed_username = fields.Char(string="Camera Username")
    feed_password = fields.Char(string="Camera Password")

    # --- Go2RTC Integration ---
    go2rtc_stream_name = fields.Char(
        string="Stream ID",
        compute="_compute_go2rtc_stream_name",
        store=True,
        help="Unique identifier used in Go2RTC",
    )
    go2rtc_status = fields.Selection(
        [
            ("inactive", "Inactive"),
            ("active", "Active"),
            ("error", "Error"),
        ],
        default="inactive",
    )
    go2rtc_error = fields.Text(string="Last Error", readonly=True)

    # --- Lifecycle ---
    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("ready", "Ready"),
            ("live", "Live"),
            ("stopped", "Stopped"),
        ],
        default="draft",
        required=True,
    )

    # --- Preview ---
    thumbnail = fields.Binary(string="Preview", attachment=True)

    # ------------------------------------------------------------------
    # Computed
    # ------------------------------------------------------------------

    @api.depends("agent_id", "agent_id.code", "company_id", "company_id.name")
    def _compute_go2rtc_stream_name(self):
        for feed in self:
            parts = []
            if feed.agent_id and feed.agent_id.code:
                parts.append(feed.agent_id.code)
            if feed.company_id and feed.company_id.name:
                slug = re.sub(r"[^a-z0-9]+", "-", feed.company_id.name.lower()).strip("-")
                parts.append(slug)
            parts.append(str(feed.id or "new"))
            feed.go2rtc_stream_name = "-".join(parts)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_rtsp_source_url(self):
        """Build the authenticated RTSP URL for Go2RTC."""
        self.ensure_one()
        url = self.rtsp_url or ""
        if self.feed_username and self.feed_password:
            # Inject credentials: rtsp://user:pass@host/path
            if "://" in url:
                proto, rest = url.split("://", 1)
                url = f"{proto}://{self.feed_username}:{self.feed_password}@{rest}"
        return url

    def _get_go2rtc_service(self):
        return self.env["daisy.go2rtc.service"]

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_activate(self):
        """Register stream in Go2RTC and move to live state."""
        self.ensure_one()
        if self.feed_type != "rtsp_in":
            raise UserError("Only camera (RTSP In) feeds can be activated.")
        if not self.rtsp_url:
            raise UserError("Set an RTSP URL before activating.")

        svc = self._get_go2rtc_service()
        source_url = self._build_rtsp_source_url()

        ok = svc.create_stream(self.go2rtc_stream_name, source_url)
        if not ok:
            self.write({
                "go2rtc_status": "error",
                "go2rtc_error": "Failed to register stream in Go2RTC",
            })
            raise UserError("Could not register stream in Go2RTC. Check logs.")

        self.write({
            "state": "live",
            "go2rtc_status": "active",
            "go2rtc_error": False,
        })
        _logger.info("Feed %s activated: %s", self.name, self.go2rtc_stream_name)

    def action_deactivate(self):
        """Remove stream from Go2RTC and stop."""
        self.ensure_one()
        svc = self._get_go2rtc_service()
        svc.delete_stream(self.go2rtc_stream_name)
        self.write({
            "state": "stopped",
            "go2rtc_status": "inactive",
            "go2rtc_error": False,
        })
        _logger.info("Feed %s deactivated", self.name)

    def action_test_connection(self):
        """Test whether the RTSP stream is reachable via Go2RTC."""
        self.ensure_one()
        if not self.rtsp_url:
            raise UserError("Set an RTSP URL first.")

        svc = self._get_go2rtc_service()
        source_url = self._build_rtsp_source_url()

        # Temporarily register stream for testing
        stream_name = f"_test_{self.go2rtc_stream_name}"
        ok = svc.create_stream(stream_name, source_url)
        if not ok:
            raise UserError("Could not connect to Go2RTC service.")

        try:
            alive = svc.test_stream(stream_name)
            if alive:
                self.write({"state": "ready", "go2rtc_error": False})
                return {
                    "type": "ir.actions.client",
                    "tag": "display_notification",
                    "params": {
                        "title": "Connection OK",
                        "message": f"Stream '{self.name}' is reachable.",
                        "type": "success",
                        "sticky": False,
                    },
                }
            else:
                self.write({
                    "go2rtc_status": "error",
                    "go2rtc_error": "Stream registered but no producers detected",
                })
                raise UserError("Stream registered but camera did not connect. Check RTSP URL and credentials.")
        finally:
            svc.delete_stream(stream_name)

    def action_refresh_thumbnail(self):
        """Grab a snapshot from the live stream and store as thumbnail."""
        self.ensure_one()
        if self.go2rtc_status != "active":
            raise UserError("Feed must be active to capture a thumbnail.")

        svc = self._get_go2rtc_service()
        jpeg_data = svc.get_snapshot(self.go2rtc_stream_name)
        if not jpeg_data:
            raise UserError("Could not grab snapshot from Go2RTC.")

        self.thumbnail = base64.b64encode(jpeg_data)

    def action_open_viewer(self):
        """Return WebSocket URL for the browser-side viewer."""
        self.ensure_one()
        if self.go2rtc_status != "active":
            raise UserError("Feed is not active.")
        svc = self._get_go2rtc_service()
        ws_url = svc.get_ws_url(self.go2rtc_stream_name)
        return {
            "type": "ir.actions.client",
            "tag": "daisy_feed_viewer",
            "params": {
                "feed_id": self.id,
                "feed_name": self.name,
                "ws_url": ws_url,
            },
        }
