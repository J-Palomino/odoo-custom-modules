import json
from odoo import http
from odoo.http import request


class DaisyAgentController(http.Controller):

    @http.route("/daisy/agents", auth="user", type="jsonrpc", methods=["POST"])
    def list_agents(self, **kw):
        """List all agents visible to the current user."""
        agents = request.env["daisy.agent"].search([])
        return [
            {
                "id": a.id,
                "name": a.name,
                "code": a.code,
                "role": a.role,
                "state": a.state,
                "active_conversations": a.active_conversations,
            }
            for a in agents
        ]

    @http.route("/daisy/agents/<int:agent_id>/status", auth="user", type="jsonrpc", methods=["POST"])
    def agent_status(self, agent_id, **kw):
        """Get status and live metrics for a single agent."""
        agent = request.env["daisy.agent"].browse(agent_id)
        if not agent.exists():
            return {"error": "Agent not found"}
        return {
            "id": agent.id,
            "name": agent.name,
            "state": agent.state,
            "active_conversations": agent.active_conversations,
            "total_conversations": agent.total_conversations,
            "total_messages_sent": agent.total_messages_sent,
        }

    # ------------------------------------------------------------------
    # Camera Feed Endpoints
    # ------------------------------------------------------------------

    @http.route("/daisy/feeds/<int:feed_id>/webrtc-url", auth="user", type="jsonrpc", methods=["POST"])
    def feed_webrtc_url(self, feed_id, **kw):
        """Return the Go2RTC WebSocket URL for a camera feed viewer."""
        feed = request.env["daisy.agent.feed"].browse(feed_id)
        if not feed.exists():
            return {"error": "Feed not found"}
        if feed.go2rtc_status != "active":
            return {"error": "Feed is not active"}

        svc = request.env["daisy.go2rtc.service"]
        ws_url = svc.get_ws_url(feed.go2rtc_stream_name)
        return {
            "feed_id": feed.id,
            "feed_name": feed.name,
            "stream_name": feed.go2rtc_stream_name,
            "ws_url": ws_url,
            "feed_type": feed.feed_type,
        }

    @http.route("/daisy/feeds/by-channel/<int:channel_id>", auth="user", type="jsonrpc", methods=["POST"])
    def feeds_by_channel(self, channel_id, **kw):
        """Return all active camera feeds for agents in a Discuss channel."""
        channel = request.env["discuss.channel"].browse(channel_id)
        if not channel.exists():
            return {"error": "Channel not found"}

        # Find agent partners in this channel
        member_partner_ids = channel.channel_member_ids.mapped("partner_id.id")
        agents = request.env["daisy.agent"].search([
            ("partner_id", "in", member_partner_ids),
            ("state", "=", "active"),
        ])

        if not agents:
            return {"feeds": []}

        feeds = request.env["daisy.agent.feed"].search([
            ("agent_id", "in", agents.ids),
            ("go2rtc_status", "=", "active"),
            ("state", "=", "live"),
        ])

        svc = request.env["daisy.go2rtc.service"]
        result = []
        for f in feeds:
            result.append({
                "feed_id": f.id,
                "feed_name": f.name,
                "agent_name": f.agent_id.name,
                "company_name": f.company_id.name if f.company_id else None,
                "feed_type": f.feed_type,
                "stream_name": f.go2rtc_stream_name,
                "ws_url": svc.get_ws_url(f.go2rtc_stream_name),
            })
        return {"feeds": result}

    @http.route("/daisy/feeds/<int:feed_id>/push-webrtc", auth="user", type="jsonrpc", methods=["POST"])
    def feed_push_webrtc(self, feed_id, sdp_offer=None, **kw):
        """Push a WebRTC stream into Go2RTC for RTSP out broadcast."""
        if not sdp_offer:
            return {"error": "sdp_offer is required"}

        feed = request.env["daisy.agent.feed"].browse(feed_id)
        if not feed.exists():
            return {"error": "Feed not found"}
        if feed.feed_type != "rtsp_out":
            return {"error": "Feed is not configured for broadcast (RTSP Out)"}

        svc = request.env["daisy.go2rtc.service"]

        # Register the output stream if not already active
        if feed.go2rtc_status != "active":
            svc.create_stream(feed.go2rtc_stream_name, "")
            feed.sudo().write({
                "go2rtc_status": "active",
                "state": "live",
            })

        sdp_answer = svc.push_sdp_offer(feed.go2rtc_stream_name, sdp_offer)
        if not sdp_answer:
            return {"error": "Failed to negotiate WebRTC with Go2RTC"}

        # Build the RTSP URL for store displays
        api_url = svc._get_api_url()
        rtsp_host = api_url.replace("http://", "").replace("https://", "").split(":")[0]
        rtsp_url = f"rtsp://{rtsp_host}:8554/{feed.go2rtc_stream_name}"

        return {
            "sdp_answer": sdp_answer,
            "rtsp_url": rtsp_url,
            "stream_name": feed.go2rtc_stream_name,
        }
