import json
import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

_AGENT_NODE = "agentAgentflow"


class DaisyOrchestratorCommand(models.Model):
    """One-shot command record the Agent Orchestrator creates (via MCP) to run
    privileged daisy-agent operations that are otherwise button methods or
    daisy.plus API calls. create() executes the command server-side and writes
    the outcome back onto the record so the caller can read it."""

    _name = "daisy.orchestrator.command"
    _description = "Daisy Orchestrator Command"
    _order = "id desc"

    command = fields.Selection(
        [
            ("onboard", "Onboard new agent"),
            ("reprovision", "Reprovision (re-key + re-clone chatflow)"),
            ("suspend", "Suspend"),
            ("reactivate", "Reactivate"),
            ("terminate", "Terminate"),
            ("set_prompt", "Set chatflow system prompt"),
            ("fix_mcp_actions", "Fix MCP tool allowlist from template"),
            ("sync_key", "Re-key chatflow (fix key 401 without re-clone)"),
        ],
        required=True,
    )
    agent_id = fields.Many2one("daisy.agent", string="Target Agent",
                               help="Agent to operate on (every command except onboard).")
    target_user_id = fields.Many2one("res.users", string="Onboard For",
                                     help="onboard: the user the new agent assists / reports to.")
    agent_first_name = fields.Char(help="onboard: optional first name (random if blank).")
    ai_personality = fields.Selection(
        [("professional", "Professional"), ("friendly", "Friendly"),
         ("technical", "Technical"), ("empathetic", "Empathetic"), ("concise", "Concise")],
        default="friendly")
    new_prompt = fields.Text(help="set_prompt: the full new system prompt.")

    status = fields.Selection([("ok", "OK"), ("error", "Error")], readonly=True)
    result_agent_id = fields.Many2one("daisy.agent", readonly=True)
    result_message = fields.Text(readonly=True)

    @api.model_create_multi
    def create(self, vals_list):
        recs = super().create(vals_list)
        for rec in recs:
            try:
                rec._run()
                if not rec.status:
                    rec.status = "ok"
            except Exception as e:  # noqa: BLE001 - report, never block the create
                _logger.exception("Orchestrator command %s failed", rec.command)
                rec.status = "error"
                rec.result_message = str(e)
        return recs

    # ---- chatflow-internals helpers (reuse daisy.ai.service) ----
    def _agent_nodes(self, flow_data_str):
        fd = json.loads(flow_data_str)
        nodes = [n for n in fd.get("nodes", []) if n.get("data", {}).get("name") == _AGENT_NODE]
        return fd, nodes

    def _edit_prompt(self, agent, new_prompt):
        ai = self.env["daisy.ai.service"]
        key, cf = agent._get_effective_api_key(), agent.daisy_agency_id
        if not cf:
            raise ValueError("Agent has no chatflow (daisy_agency_id).")
        fd, nodes = self._agent_nodes(ai.get_chatflow(key, cf)["flowData"])
        if not nodes:
            raise ValueError("No agent node found in chatflow.")
        for n in nodes:
            msgs = n["data"]["inputs"].get("agentMessages") or [{"role": "system", "content": ""}]
            msgs[0]["content"] = new_prompt
            n["data"]["inputs"]["agentMessages"] = msgs
        ai.update_chatflow(key, cf, {"flowData": json.dumps(fd)})

    def _fix_mcp_actions(self, agent):
        ai = self.env["daisy.ai.service"]
        ICP = self.env["ir.config_parameter"].sudo()
        template_id = ICP.get_param("daisy.template_chatflow_id", "").strip()
        if not template_id:
            raise ValueError("No daisy.template_chatflow_id configured.")
        # canonical allowlist from the maintained template (read with the workspace key)
        _, tnodes = self._agent_nodes(ai.get_chatflow(agent._workspace_key(), template_id)["flowData"])
        tmpl_actions = None
        for n in tnodes:
            for t in n["data"]["inputs"].get("agentTools", []) or []:
                cfg = t.get("agentSelectedToolConfig", {})
                if cfg.get("mcpActions"):
                    tmpl_actions = cfg["mcpActions"]
        if not tmpl_actions:
            raise ValueError("Template chatflow has no mcpActions to copy.")
        key, cf = agent._get_effective_api_key(), agent.daisy_agency_id
        fd, nodes = self._agent_nodes(ai.get_chatflow(key, cf)["flowData"])
        applied = 0
        for n in nodes:
            for t in n["data"]["inputs"].get("agentTools", []) or []:
                cfg = t.get("agentSelectedToolConfig", {})
                if "mcpActions" in cfg:
                    cfg["mcpActions"] = tmpl_actions
                    applied += 1
        if not applied:
            raise ValueError("Target chatflow has no MCP tool config to fix.")
        ai.update_chatflow(key, cf, {"flowData": json.dumps(fd)})
        return applied

    def _run(self):
        self.ensure_one()
        cmd = self.command
        if cmd == "onboard":
            if not self.target_user_id:
                raise ValueError("onboard requires target_user_id.")
            wiz = self.env["daisy.agent.create.for.user"].create({
                "target_user_id": self.target_user_id.id,
                "agent_first_name": self.agent_first_name or False,
                "ai_personality": self.ai_personality or "friendly",
            })
            action = wiz.action_create_agent()
            agent = self.env["daisy.agent"].browse(action["res_id"])
            agent.action_hire()
            self.result_agent_id = agent.id
            self.result_message = (
                f"Onboarded agent {agent.id} '{agent.name}' (user {agent.user_id.login}, "
                f"chatflow {agent.daisy_agency_id}, provision_state={agent.provision_state})."
            )
            return
        agent = self.agent_id
        if not agent:
            raise ValueError(f"{cmd} requires agent_id.")
        if cmd == "reprovision":
            agent.action_reprovision()
            self.result_message = f"Reprovisioned {agent.name}: provision_state={agent.provision_state}."
        elif cmd == "suspend":
            agent.action_suspend()
            self.result_message = f"Suspended {agent.name}."
        elif cmd == "reactivate":
            agent.action_reactivate()
            self.result_message = f"Reactivated {agent.name}."
        elif cmd == "terminate":
            agent.action_terminate()
            self.result_message = f"Terminated {agent.name}."
        elif cmd == "set_prompt":
            if not self.new_prompt:
                raise ValueError("set_prompt requires new_prompt.")
            self._edit_prompt(agent, self.new_prompt)
            self.result_message = f"Updated system prompt for {agent.name} (chatflow {agent.daisy_agency_id})."
        elif cmd == "fix_mcp_actions":
            applied = self._fix_mcp_actions(agent)
            self.result_message = f"Reset MCP allowlist on {agent.name} from template ({applied} config(s))."
        elif cmd == "sync_key":
            ai = self.env["daisy.ai.service"]
            key = agent._get_effective_api_key() or agent._workspace_key()
            cf = agent.daisy_agency_id
            if not cf:
                raise ValueError("Agent has no chatflow (daisy_agency_id).")
            info = ai.create_apikey(key, f"agent:{agent.code}")
            new_key, key_id = info.get("apiKey"), info.get("id")
            if not (new_key and key_id):
                raise ValueError("create_apikey returned no usable key.")
            ai.link_apikey(key, cf, key_id)
            agent.write({"daisy_api_key": new_key})
            self.result_message = f"Re-keyed {agent.name}: minted + linked fresh gating key on chatflow {cf}."
        else:
            raise ValueError(f"Unknown command {cmd}.")
