import logging

import requests
from odoo import models, api

_logger = logging.getLogger(__name__)

DEFAULT_GO2RTC_API_URL = "https://go2rtc-media-production.up.railway.app"
DEFAULT_GO2RTC_PUBLIC_URL = "https://go2rtc-media-production.up.railway.app"


class DaisyGo2RTCService(models.AbstractModel):
    """Wrap Go2RTC REST API for stream management."""

    _name = "daisy.go2rtc.service"
    _description = "Go2RTC Stream Service"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @api.model
    def _get_api_url(self):
        return (
            self.env["ir.config_parameter"]
            .sudo()
            .get_param("daisy.go2rtc_api_url", DEFAULT_GO2RTC_API_URL)
        )

    @api.model
    def _get_public_url(self):
        return (
            self.env["ir.config_parameter"]
            .sudo()
            .get_param("daisy.go2rtc_public_url", DEFAULT_GO2RTC_PUBLIC_URL)
        )

    # ------------------------------------------------------------------
    # Stream CRUD
    # ------------------------------------------------------------------

    @api.model
    def create_stream(self, name, source_url):
        """Register a stream in Go2RTC.

        PUT /api/streams?name={name}&src={source_url}
        """
        url = f"{self._get_api_url()}/api/streams"
        try:
            resp = requests.put(
                url,
                params={"name": name, "src": source_url},
                timeout=10,
            )
            resp.raise_for_status()
            _logger.info("Go2RTC stream created: %s -> %s", name, source_url)
            return True
        except requests.RequestException as exc:
            _logger.error("Go2RTC create_stream failed for %s: %s", name, exc)
            return False

    @api.model
    def delete_stream(self, name):
        """Remove a stream from Go2RTC.

        DELETE /api/streams?src={name}
        """
        url = f"{self._get_api_url()}/api/streams"
        try:
            resp = requests.delete(url, params={"src": name}, timeout=10)
            resp.raise_for_status()
            _logger.info("Go2RTC stream deleted: %s", name)
            return True
        except requests.RequestException as exc:
            _logger.error("Go2RTC delete_stream failed for %s: %s", name, exc)
            return False

    @api.model
    def list_streams(self):
        """List all registered streams.

        GET /api/streams
        Returns dict of {name: [producers]}
        """
        url = f"{self._get_api_url()}/api/streams"
        try:
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            _logger.error("Go2RTC list_streams failed: %s", exc)
            return {}

    @api.model
    def get_snapshot(self, name):
        """Grab a JPEG snapshot from a live stream.

        GET /api/frame.jpeg?src={name}
        Returns raw JPEG bytes or None.
        """
        url = f"{self._get_api_url()}/api/frame.jpeg"
        try:
            resp = requests.get(url, params={"src": name}, timeout=15)
            resp.raise_for_status()
            return resp.content
        except requests.RequestException as exc:
            _logger.error("Go2RTC get_snapshot failed for %s: %s", name, exc)
            return None

    @api.model
    def test_stream(self, name):
        """Check whether a stream has active producers.

        Returns True if the stream exists and has at least one producer.
        """
        streams = self.list_streams()
        stream_info = streams.get(name)
        if not stream_info:
            return False
        # stream_info is a list of producer dicts
        if isinstance(stream_info, list):
            return len(stream_info) > 0
        return bool(stream_info)

    @api.model
    def get_ws_url(self, name):
        """Return the public WebSocket URL for browser playback.

        ws(s)://host/api/ws?src={name}
        """
        public = self._get_public_url().rstrip("/")
        scheme = "wss" if public.startswith("https") else "ws"
        host = public.split("://", 1)[-1]
        return f"{scheme}://{host}/api/ws?src={name}"

    @api.model
    def get_webrtc_url(self, name):
        """Return the public WebRTC WHIP URL for a stream.

        Used for pushing media into Go2RTC.
        POST /api/webrtc?dst={name}
        """
        public = self._get_public_url().rstrip("/")
        return f"{public}/api/webrtc?dst={name}"

    @api.model
    def push_sdp_offer(self, name, sdp_offer):
        """Send an SDP offer to Go2RTC WHIP endpoint and return the SDP answer.

        POST /api/webrtc?dst={name}
        Content-Type: application/sdp
        Body: SDP offer
        Returns: SDP answer string or None on error.
        """
        url = f"{self._get_api_url()}/api/webrtc"
        try:
            resp = requests.post(
                url,
                params={"dst": name},
                data=sdp_offer,
                headers={"Content-Type": "application/sdp"},
                timeout=10,
            )
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as exc:
            _logger.error("Go2RTC push_sdp_offer failed for %s: %s", name, exc)
            return None
