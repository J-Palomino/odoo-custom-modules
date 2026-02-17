import logging
import requests
from odoo import models, api

_logger = logging.getLogger(__name__)

DAISY_API_BASE_DEFAULT = "https://daisy.plus/api/v1"


class DaisyAIService(models.AbstractModel):
    """AI service for Daisy+ Chat - connects to Daisy server"""
    _name = "daisy.ai.service"
    _description = "Daisy+ AI Service"

    @api.model
    def _get_daisy_api_base(self):
        """Get Daisy API base URL from system parameter, with sensible default."""
        return (
            self.env["ir.config_parameter"]
            .sudo()
            .get_param("daisy.api_base_url", DAISY_API_BASE_DEFAULT)
            .rstrip("/")
        )

    @api.model
    def get_ai_response(self, channel_id, message, conversation_history=None, conversation_id=None):
        """
        Get AI response from Daisy+ API.

        Args:
            channel_id: im_livechat.channel ID
            message: User message text
            conversation_history: List of previous messages
            conversation_id: Existing conversation ID (for continuity)

        Returns:
            dict: {
                "response": str,
                "confidence": float,
                "intent": str,
                "should_handoff": bool,
                "suggested_actions": list,
                "conversation_id": str
            }
        """
        channel = self.env["im_livechat.channel"].browse(channel_id)

        if not channel.ai_enabled:
            return {"response": None, "should_handoff": True}

        if not channel.daisy_api_key:
            _logger.warning(f"Daisy+ API key not configured for channel {channel_id}")
            return {"response": None, "should_handoff": True}

        if not channel.daisy_agency_id:
            _logger.warning(f"Daisy+ Chatflow not selected for channel {channel_id}")
            return {"response": None, "should_handoff": True}

        return self._call_daisy_api(channel, message, conversation_history, conversation_id)

    def _call_daisy_api(self, channel, message, history, conversation_id):
        """Call Daisy+ prediction API for chat response."""
        try:
            base_url = self._get_daisy_api_base()
            agency_id = channel.daisy_agency_id

            payload = {
                "question": message,
            }
            if conversation_id:
                payload["chatId"] = conversation_id

            response = requests.post(
                f"{base_url}/prediction/{agency_id}",
                json=payload,
                headers={
                    "Authorization": f"Bearer {channel.daisy_api_key}",
                    "Content-Type": "application/json",
                },
                timeout=30
            )
            response.raise_for_status()
            data = response.json()

            confidence = 0.9
            should_handoff = confidence < channel.ai_handoff_threshold

            return {
                "response": data.get("text", ""),
                "confidence": confidence,
                "intent": data.get("intent"),
                "should_handoff": should_handoff,
                "suggested_actions": data.get("suggested_actions", []),
                "conversation_id": data.get("chatId", conversation_id),
            }

        except requests.exceptions.Timeout:
            _logger.error("Daisy+ API timeout")
            return {
                "response": "I'm having trouble connecting right now. Let me get a human to help you.",
                "confidence": 0,
                "should_handoff": True,
            }
        except requests.exceptions.RequestException as e:
            _logger.error(f"Daisy+ API error: {e}")
            return {
                "response": None,
                "should_handoff": True,
                "error": str(e),
            }

    @api.model
    def fetch_agencies(self, api_key):
        """
        Fetch available chatflows/agencies for an API key.

        Args:
            api_key: Daisy+ API key

        Returns:
            list: List of chatflow dicts with 'id', 'name', etc.
        """
        try:
            base_url = self._get_daisy_api_base()
            response = requests.get(
                f"{base_url}/chatflows",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                timeout=10
            )
            response.raise_for_status()
            data = response.json()

            # Server returns array of chatflow objects directly
            if isinstance(data, list):
                return data
            return data.get("agencies", data.get("data", []))

        except requests.exceptions.RequestException as e:
            _logger.error(f"Failed to fetch agencies: {e}")
            return []

    @api.model
    def validate_api_key(self, api_key):
        """
        Validate a Daisy+ API key using the verify endpoint.

        Args:
            api_key: Daisy+ API key

        Returns:
            dict: {"valid": bool, "error": str}
        """
        try:
            base_url = self._get_daisy_api_base()
            response = requests.get(
                f"{base_url}/verify/apikey/{api_key}",
                timeout=10
            )

            if response.status_code == 200:
                return {"valid": True}

            if response.status_code == 401:
                return {"valid": False, "error": "Invalid API key"}

            response.raise_for_status()
            return {"valid": False, "error": f"Unexpected status: {response.status_code}"}

        except requests.exceptions.RequestException as e:
            _logger.error(f"API key validation error: {e}")
            return {"valid": False, "error": str(e)}
