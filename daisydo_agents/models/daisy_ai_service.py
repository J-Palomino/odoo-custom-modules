import logging
import uuid

import requests
from odoo import models, api

_logger = logging.getLogger(__name__)


class DaisyAIServiceAgent(models.AbstractModel):
    """Extend AI service with agent-level API calls."""
    _inherit = "daisy.ai.service"

    @api.model
    def _call_daisy_api_for_agent(self, agent, message, history=None, conversation_id=None, override_config=None):
        """Call Daisy+ prediction API using an agent's own credentials."""
        if not agent.daisy_api_key:
            return {"response": None, "should_handoff": True}

        if not agent.daisy_agency_id:
            _logger.warning("No chatflow ID configured for agent %s", agent.name)
            return {"response": None, "should_handoff": True}

        request_id = uuid.uuid4().hex[:12]
        try:
            base_url = self._get_daisy_api_base()
            agency_id = agent.daisy_agency_id

            payload = {
                "question": message,
            }
            if conversation_id:
                payload["chatId"] = conversation_id

            # Build overrideConfig from agent settings
            ov = dict(override_config or {})
            if agent.ai_system_prompt:
                personality_prefix = ""
                if agent.ai_personality:
                    personality_prefix = f"Respond in a {agent.ai_personality} tone.\n\n"
                ov["systemMessagePrompt"] = personality_prefix + agent.ai_system_prompt
            elif agent.ai_personality:
                ov["systemMessagePrompt"] = f"Respond in a {agent.ai_personality} tone."
            if ov:
                payload["overrideConfig"] = ov

            _logger.info(
                "[%s] Daisy+ API request for agent %s -> %s/prediction/%s",
                request_id, agent.name, base_url, agency_id,
            )

            response = requests.post(
                f"{base_url}/prediction/{agency_id}",
                json=payload,
                headers={
                    "Authorization": f"Bearer {agent.daisy_api_key}",
                    "Content-Type": "application/json",
                },
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()

            confidence = data.get("confidence", 0.9)
            should_handoff = confidence < agent.ai_handoff_threshold

            return {
                "response": data.get("text", ""),
                "confidence": confidence,
                "intent": data.get("intent"),
                "should_handoff": should_handoff,
                "suggested_actions": data.get("suggested_actions", []),
                "conversation_id": data.get("chatId", conversation_id),
            }

        except requests.exceptions.Timeout:
            _logger.error("[%s] Daisy+ API timeout for agent %s", request_id, agent.name)
            return {
                "response": "I'm having trouble connecting right now. Let me get a human to help you.",
                "confidence": 0,
                "should_handoff": True,
            }
        except requests.exceptions.RequestException as e:
            _logger.error("[%s] Daisy+ API error for agent %s: %s", request_id, agent.name, e)
            return {"response": None, "should_handoff": True, "error": str(e)}
