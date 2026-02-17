import logging
import re

from odoo import models, fields, api
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class DaisyAgentImport(models.TransientModel):
    _name = "daisy.agent.import"
    _description = "Import Agents from Daisy+"

    daisy_api_key = fields.Char(string="Daisy+ API Key", required=True)
    state = fields.Selection(
        [("key", "Enter Key"), ("select", "Select Agents")],
        default="key",
        required=True,
    )
    line_ids = fields.One2many(
        "daisy.agent.import.line", "wizard_id", string="Available Agents"
    )
    import_count = fields.Integer(compute="_compute_counts")
    skip_count = fields.Integer(compute="_compute_counts")

    @api.depends("line_ids.selected", "line_ids.already_imported")
    def _compute_counts(self):
        for wiz in self:
            wiz.import_count = len(
                wiz.line_ids.filtered(lambda l: l.selected and not l.already_imported)
            )
            wiz.skip_count = len(wiz.line_ids.filtered(lambda l: l.already_imported))

    def action_fetch_agents(self):
        self.ensure_one()
        ai_svc = self.env["daisy.ai.service"]

        # Validate key
        result = ai_svc.validate_api_key(self.daisy_api_key)
        if not result.get("valid"):
            raise UserError(
                f"Invalid API key: {result.get('error', 'unknown error')}"
            )

        # Fetch chatflows
        chatflows = ai_svc.fetch_agencies(self.daisy_api_key)
        if not chatflows:
            raise UserError(
                "No chatflows found for this API key. "
                "Create chatflows in Daisy+ first."
            )

        # Find already-imported agency IDs
        existing_ids = set(
            self.env["daisy.agent"]
            .search([("daisy_agency_id", "in", [c.get("id", "") for c in chatflows])])
            .mapped("daisy_agency_id")
        )

        # Clear old lines and create new ones
        self.line_ids.unlink()
        lines = []
        for cf in chatflows:
            cf_id = cf.get("id", "")
            lines.append((0, 0, {
                "chatflow_id": cf_id,
                "name": cf.get("name", "Unnamed Agent"),
                "description": cf.get("description", ""),
                "already_imported": cf_id in existing_ids,
                "selected": cf_id not in existing_ids,
            }))

        self.write({"state": "select", "line_ids": lines})

        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }

    def action_import_selected(self):
        self.ensure_one()
        to_import = self.line_ids.filtered(
            lambda l: l.selected and not l.already_imported
        )
        if not to_import:
            raise UserError("No new agents selected for import.")

        created = self.env["daisy.agent"]
        for line in to_import:
            code = self._generate_unique_code(line.name)
            agent = self.env["daisy.agent"].create({
                "name": line.name,
                "code": code,
                "role": line.description or "AI Agent",
                "daisy_api_key": self.daisy_api_key,
                "daisy_agency_id": line.chatflow_id,
                "state": "draft",
            })
            created |= agent
            _logger.info(
                "Imported agent '%s' (chatflow %s) as %s",
                line.name, line.chatflow_id, code,
            )

        return {
            "type": "ir.actions.act_window",
            "name": "Agents",
            "res_model": "daisy.agent",
            "view_mode": "kanban,list,form",
            "target": "current",
        }

    def action_select_all(self):
        self.ensure_one()
        self.line_ids.filtered(lambda l: not l.already_imported).write(
            {"selected": True}
        )
        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }

    def action_deselect_all(self):
        self.ensure_one()
        self.line_ids.filtered(lambda l: not l.already_imported).write(
            {"selected": False}
        )
        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }

    def _generate_unique_code(self, name):
        """Slugify name into a unique agent code (max 30 chars)."""
        slug = re.sub(r"[^a-z0-9]+", "-", (name or "agent").lower()).strip("-")[:30]
        if not slug:
            slug = "agent"
        code = slug
        suffix = 2
        while self.env["daisy.agent"].search_count([("code", "=", code)]):
            max_len = 30 - len(f"-{suffix}")
            code = f"{slug[:max_len]}-{suffix}"
            suffix += 1
        return code


class DaisyAgentImportLine(models.TransientModel):
    _name = "daisy.agent.import.line"
    _description = "Import Wizard Line"

    wizard_id = fields.Many2one(
        "daisy.agent.import", required=True, ondelete="cascade"
    )
    selected = fields.Boolean(default=True)
    chatflow_id = fields.Char(string="Chatflow ID", readonly=True)
    name = fields.Char(readonly=True)
    description = fields.Char(readonly=True)
    already_imported = fields.Boolean(readonly=True)
