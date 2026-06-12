import json
import logging
import re
import uuid

import requests
from odoo import models, api

_logger = logging.getLogger(__name__)


class DaisyAIServiceAgent(models.AbstractModel):
    """Extend AI service with agent-level API calls."""
    _inherit = "daisy.ai.service"

    @api.model
    def _resolve_agent_credentials(self, agent):
        """Resolve API key and agency ID for an agent, falling back to global params."""
        api_key = agent.daisy_api_key
        agency_id = agent.daisy_agency_id

        if not api_key:
            # Fall back to global system parameter
            ICP = self.env["ir.config_parameter"].sudo()
            api_key = ICP.get_param("daisy_bot.api_key", "")
            if api_key:
                _logger.info(
                    "Agent %s has no API key — using global daisy_bot.api_key",
                    agent.name,
                )

        if not agency_id:
            ICP = self.env["ir.config_parameter"].sudo()
            # Fall back to agency ID embedded in the global api_url
            global_url = ICP.get_param("daisy_bot.api_url", "")
            if "/prediction/" in global_url:
                agency_id = global_url.rsplit("/prediction/", 1)[-1].strip("/")
                _logger.info(
                    "Agent %s has no agency ID — extracted %s from global api_url",
                    agent.name, agency_id,
                )

        return api_key, agency_id

    @api.model
    def _call_daisy_api_for_agent(self, agent, message, history=None, conversation_id=None, override_config=None, session_id=None):
        """Call Daisy+ prediction API using an agent's own credentials."""
        api_key, agency_id = self._resolve_agent_credentials(agent)

        if not api_key:
            _logger.warning(
                "Agent %s: no API key configured and no global fallback. "
                "Set daisy_api_key on the agent or daisy_bot.api_key in System Parameters.",
                agent.name,
            )
            return {
                "response": "I'm not fully configured yet — a team member will help you shortly.",
                "should_handoff": True,
                "error": "no_api_key",
            }

        if not agency_id:
            _logger.warning("No chatflow ID configured for agent %s", agent.name)
            return {
                "response": "I'm not fully configured yet — a team member will help you shortly.",
                "should_handoff": True,
                "error": "no_agency_id",
            }

        request_id = uuid.uuid4().hex[:12]
        try:
            base_url = self._get_daisy_api_base()

            payload = {
                "question": message,
            }
            if conversation_id:
                payload["chatId"] = conversation_id
            merged_override = dict(override_config or {})
            if session_id:
                merged_override["sessionId"] = session_id
            if merged_override:
                payload["overrideConfig"] = merged_override

            _logger.info(
                "[%s] Daisy+ API request for agent %s → %s/prediction/%s",
                request_id, agent.name, base_url, agency_id,
            )

            response = requests.post(
                f"{base_url}/prediction/{agency_id}",
                json=payload,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                # Generous: agency runs that capture screenshots / run Playwright
                # over the MeshCentral relay can take well over a minute.
                timeout=180,
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
                "images": self._extract_response_images(data),
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

    @api.model
    def _extract_response_images(self, data):
        """Pull base64 image data-URIs out of a Daisy+ prediction response.

        Image-producing MCP tools (e.g. MeshCentral devices_screenshot) surface
        their output inside ``agentFlowExecutedData``/artifacts as a
        ``data:image/<type>;base64,<...>`` URI rather than in ``text``. We scan
        the serialized response so the worker can attach the image(s) to the
        posted message. Returns a list of ``{"mime", "b64"}`` dicts.
        """
        try:
            raw = json.dumps(data)
        except Exception:
            return []
        images, seen = [], set()
        for m in re.finditer(r"data:image/([a-zA-Z0-9.+-]+);base64,([A-Za-z0-9+/=]+)", raw):
            b64 = m.group(2)
            if len(b64) < 256:  # skip tiny inline icons/spinners
                continue
            dedup = b64[:64]
            if dedup in seen:
                continue
            seen.add(dedup)
            images.append({"mime": "image/" + m.group(1).lower(), "b64": b64})
            if len(images) >= 5:
                break
        return images
