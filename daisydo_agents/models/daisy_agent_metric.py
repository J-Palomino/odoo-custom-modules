from odoo import models, fields, api


class DaisyAgentMetric(models.Model):
    _name = "daisy.agent.metric"
    _description = "Agent Daily Metrics"
    _order = "date desc"

    agent_id = fields.Many2one("daisy.agent", required=True, ondelete="cascade", index=True)
    date = fields.Date(required=True, default=fields.Date.context_today, index=True)

    conversations_handled = fields.Integer()
    conversations_resolved = fields.Integer()
    conversations_handed_off = fields.Integer()
    messages_sent = fields.Integer()
    avg_response_time = fields.Float(string="Avg Response Time (s)")
    avg_confidence = fields.Float()
    resolution_rate = fields.Float(compute="_compute_resolution_rate", store=True)

    agent_date_unique = models.Constraint(
        "UNIQUE(agent_id, date)",
        "One metric record per agent per day.",
    )

    @api.depends("conversations_handled", "conversations_resolved")
    def _compute_resolution_rate(self):
        for m in self:
            m.resolution_rate = (
                (m.conversations_resolved / m.conversations_handled * 100)
                if m.conversations_handled
                else 0.0
            )

    @api.model
    def _cron_snapshot_metrics(self):
        """Daily cron: create metric snapshots for all active agents."""
        today = fields.Date.context_today(self)
        agents = self.env["daisy.agent"].search([("state", "=", "active")])
        for agent in agents:
            existing = self.search([("agent_id", "=", agent.id), ("date", "=", today)], limit=1)
            if existing:
                continue
            # Count today's messages for this agent
            domain_msgs = [
                ("author_id", "=", agent.partner_id.id),
                ("daisy_ai_generated", "=", True),
                ("date", ">=", fields.Datetime.to_string(
                    fields.Datetime.start_of(fields.Datetime.now(), "day")
                )),
            ]
            messages_sent = self.env["mail.message"].search_count(domain_msgs)
            self.create({
                "agent_id": agent.id,
                "date": today,
                "messages_sent": messages_sent,
            })
