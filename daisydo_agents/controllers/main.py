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
