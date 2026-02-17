import json
import logging
from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class DaisyChatController(http.Controller):
    """API endpoints for Daisy+ Chat embeddable widget."""

    @http.route("/daisy/chat/init", type="jsonrpc", auth="public", cors="*")
    def chat_init(self, channel_uuid=None, **kwargs):
        """
        Initialize chat widget - returns configuration and creates session.

        Args:
            channel_uuid: Livechat channel UUID

        Returns:
            dict: Widget configuration and session info
        """
        channel = request.env["im_livechat.channel"].sudo().search([
            ("uuid", "=", channel_uuid)
        ], limit=1)

        if not channel:
            return {"error": "Channel not found"}

        return {
            "success": True,
            "config": {
                "channel_id": channel.id,
                "channel_uuid": channel.uuid,
                "widget_position": channel.widget_position,
                "primary_color": channel.widget_primary_color,
                "header_text": channel.widget_header_text,
                "placeholder": channel.widget_placeholder,
                "ai_enabled": channel.ai_enabled,
                "auto_greet": {
                    "enabled": channel.auto_greet_enabled,
                    "delay": channel.auto_greet_delay,
                    "message": channel.auto_greet_message,
                },
            },
        }

    @http.route("/daisy/chat/message", type="jsonrpc", auth="public", cors="*")
    def chat_message(self, channel_id, message, session_id=None, **kwargs):
        """
        Send message and get AI response.

        Args:
            channel_id: Livechat channel ID
            message: User message text
            session_id: Chat session ID (optional)

        Returns:
            dict: AI response and metadata
        """
        ai_service = request.env["daisy.ai.service"].sudo()

        try:
            response = ai_service.get_ai_response(
                channel_id=channel_id,
                message=message,
                conversation_history=kwargs.get("history", [])
            )

            return {
                "success": True,
                "response": response.get("response"),
                "ai_generated": True,
                "confidence": response.get("confidence", 0),
                "intent": response.get("intent"),
                "should_handoff": response.get("should_handoff", False),
                "suggested_actions": response.get("suggested_actions", []),
            }

        except Exception as e:
            _logger.error(f"Daisy Chat error: {e}")
            return {
                "success": False,
                "error": "Failed to process message",
                "should_handoff": True,
            }

    @http.route("/daisy/chat/handoff", type="jsonrpc", auth="public", cors="*")
    def chat_handoff(self, channel_id, session_id, conversation_history=None, **kwargs):
        """
        Escalate chat to human agent.

        Args:
            channel_id: Livechat channel ID
            session_id: Chat session ID
            conversation_history: Previous messages for context

        Returns:
            dict: Handoff status and operator info
        """
        channel = request.env["im_livechat.channel"].sudo().browse(channel_id)

        if not channel.exists():
            return {"error": "Channel not found"}

        # Get available operator
        available_operators = channel.available_operator_ids
        operator = available_operators[0] if available_operators else None

        return {
            "success": True,
            "handoff_initiated": True,
            "operator_available": bool(operator),
            "operator_name": operator.name if operator else None,
            "message": "Connecting you with a team member..." if operator else "All agents are currently busy. Please leave a message.",
            "queue_position": 0 if operator else 1,
        }

    @http.route("/daisy/chat/feedback", type="jsonrpc", auth="public", cors="*")
    def chat_feedback(self, session_id, rating, comment=None, **kwargs):
        """
        Submit chat feedback/rating.

        Args:
            session_id: Chat session ID
            rating: 1-5 star rating
            comment: Optional feedback comment

        Returns:
            dict: Confirmation
        """
        # Store feedback logic here
        _logger.info(f"Chat feedback: session={session_id}, rating={rating}, comment={comment}")

        return {
            "success": True,
            "message": "Thank you for your feedback!",
        }

    @http.route("/daisy/chat/widget.js", type="http", auth="public", cors="*")
    def widget_embed_script(self, channel_uuid=None, **kwargs):
        """
        Serve embeddable widget JavaScript.

        Usage:
            <script src="https://your-domain.com/daisy/chat/widget.js?channel_uuid=xxx"></script>
        """
        safe_host_url = json.dumps(request.httprequest.host_url)
        safe_channel_uuid = json.dumps(channel_uuid or "")
        script = f"""
(function() {{
    var DAISY_CHAT_CONFIG = {{
        serverUrl: {safe_host_url},
        channelUuid: {safe_channel_uuid},
    }};

    // Load main widget script
    var script = document.createElement('script');
    script.src = DAISY_CHAT_CONFIG.serverUrl + 'daisydo_livechat/static/src/js/daisy_chat_embed.js';
    script.async = true;
    script.onload = function() {{
        if (window.DaisyChat) {{
            window.DaisyChat.init(DAISY_CHAT_CONFIG);
        }}
    }};
    document.head.appendChild(script);

    // Load widget styles
    var link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = DAISY_CHAT_CONFIG.serverUrl + 'daisydo_livechat/static/src/css/daisy_chat_embed.css';
    document.head.appendChild(link);
}})();
"""
        return request.make_response(
            script,
            headers=[("Content-Type", "application/javascript")]
        )
