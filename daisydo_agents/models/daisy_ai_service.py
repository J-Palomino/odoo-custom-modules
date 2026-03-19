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
            if override_config:
                payload["overrideConfig"] = override_config

            _logger.info(
                "[%s] Daisy+ API request for agent %s → %s/prediction/%s",
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

    @api.model
    def get_chatflow(self, api_key, chatflow_id):
        """Fetch a full chatflow definition including flowData nodes."""
        base_url = self._get_daisy_api_base()
        try:
            resp = requests.get(
                f"{base_url}/chatflows/{chatflow_id}",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=15,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
            _logger.error("Failed to fetch chatflow %s: %s", chatflow_id, e)
            return None

    @api.model
    def clone_chatflow(self, api_key, template_id, new_name, mcp_credentials=None):
        """Clone a template chatflow and inject MCP credentials.

        1. GET the template chatflow (full flowData)
        2. Modify: new name, new node IDs, inject credentials
        3. POST as a new chatflow
        Returns the new chatflow ID or None on failure.
        """
        import copy
        import json
        import uuid as _uuid

        base_url = self._get_daisy_api_base()

        # Step 1: Fetch template
        template = self.get_chatflow(api_key, template_id)
        if not template:
            _logger.error("Could not fetch template chatflow %s", template_id)
            return None

        # Step 2: Deep copy and modify
        new_flow = copy.deepcopy(template)

        # Strip fields that shouldn't be copied
        for key in ("id", "chatflowId", "createdDate", "updatedDate", "chatbotConfig"):
            new_flow.pop(key, None)

        new_flow["name"] = new_name

        # Parse flowData if it's a string
        flow_data = new_flow.get("flowData")
        if isinstance(flow_data, str):
            try:
                flow_data = json.loads(flow_data)
            except (json.JSONDecodeError, TypeError):
                flow_data = None

        if flow_data and mcp_credentials:
            # Walk all nodes and inject MCP credentials where applicable
            nodes = flow_data.get("nodes", [])
            for node in nodes:
                data = node.get("data", {})
                inputs = data.get("inputs", {})

                # Regenerate node IDs to avoid conflicts
                old_id = node.get("id")
                new_id = f"{node.get('type', 'node')}_{_uuid.uuid4().hex[:8]}"
                node["id"] = new_id

                # Update edge references
                for edge in flow_data.get("edges", []):
                    if edge.get("source") == old_id:
                        edge["source"] = new_id
                    if edge.get("target") == old_id:
                        edge["target"] = new_id

                # Inject MCP credentials into any node that has relevant fields
                # Common patterns: mcpServer, headers, serverUrl, apiKey fields
                self._inject_mcp_credentials(inputs, mcp_credentials)
                # Also check top-level data for credential fields
                self._inject_mcp_credentials(data, mcp_credentials)

            new_flow["flowData"] = json.dumps(flow_data)

        # Step 3: POST as new chatflow
        try:
            resp = requests.post(
                f"{base_url}/chatflows",
                json=new_flow,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                timeout=15,
            )
            resp.raise_for_status()
            result = resp.json()
            new_id = result.get("id") or result.get("chatflowId")
            _logger.info(
                "Cloned chatflow '%s' from template %s → new id %s",
                new_name, template_id, new_id,
            )
            return new_id
        except requests.exceptions.RequestException as e:
            _logger.error("Failed to create cloned chatflow: %s", e)
            return None

    @staticmethod
    def _inject_mcp_credentials(data_dict, creds):
        """Inject MCP credentials into a node's data/inputs dict.

        Looks for keys that match common MCP/Odoo patterns and replaces
        their values with the new credentials.
        """
        if not isinstance(data_dict, dict):
            return

        # Map of patterns → credential values to inject
        # Keys are lowercase for case-insensitive matching
        inject_map = {
            "odoo_url": creds.get("odoo_url", ""),
            "odoo_username": creds.get("odoo_username", ""),
            "odoo_api_key": creds.get("odoo_api_key", ""),
            "x-odoo-username": creds.get("odoo_username", ""),
            "x-odoo-api-key": creds.get("odoo_api_key", ""),
            "serverurl": creds.get("mcp_server_url", ""),
            "mcpserverurl": creds.get("mcp_server_url", ""),
        }

        for key in list(data_dict.keys()):
            lower_key = key.lower().replace("_", "").replace("-", "")
            # Direct field name matches
            for pattern, value in inject_map.items():
                clean_pattern = pattern.lower().replace("_", "").replace("-", "")
                if lower_key == clean_pattern and value:
                    data_dict[key] = value

            # Handle nested headers (JSON string or dict)
            if "header" in key.lower():
                val = data_dict[key]
                if isinstance(val, str):
                    try:
                        headers = __import__("json").loads(val)
                    except (ValueError, TypeError):
                        headers = None
                    if isinstance(headers, dict):
                        _update_headers(headers, creds)
                        data_dict[key] = __import__("json").dumps(headers)
                elif isinstance(val, dict):
                    _update_headers(val, creds)


def _update_headers(headers, creds):
    """Update Odoo auth headers in a headers dict."""
    for k in list(headers.keys()):
        if k.lower() == "x-odoo-username" and creds.get("odoo_username"):
            headers[k] = creds["odoo_username"]
        elif k.lower() == "x-odoo-api-key" and creds.get("odoo_api_key"):
            headers[k] = creds["odoo_api_key"]
