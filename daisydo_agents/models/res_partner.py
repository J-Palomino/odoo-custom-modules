from odoo import api, models


class ResPartner(models.Model):
    _inherit = "res.partner"

    @api.model
    @api.readonly
    def get_mention_suggestions(self, *args, **kwargs):
        """Hide PRIVATE daisy agents from Discuss @mention / new-DM suggestions
        for everyone except their designated owner. Together with the auto-reply
        gate on the agent, this makes a private agent both invisible and
        unreachable to non-owners. Normal (non-private) agents are unaffected.

        Note: in Odoo 19 get_mention_suggestions returns a Store dict keyed by
        model name; hr embeds the employee card *inside* each res.partner entry,
        so stripping the partner also removes its employee card — no separate
        hr.employee handling needed."""
        res = super().get_mention_suggestions(*args, **kwargs)

        hidden_agents = self.env["daisy.agent"].sudo().search([
            ("x_private_owner_id", "!=", False),
            ("x_private_owner_id", "!=", self.env.user.id),
        ])
        if not hidden_agents:
            return res
        hidden_partner_ids = set(hidden_agents.mapped("partner_id").ids)

        def _strip(entries):
            return [e for e in entries
                    if not (isinstance(e, dict) and e.get("id") in hidden_partner_ids)]

        if isinstance(res, dict) and res.get("res.partner"):
            res["res.partner"] = _strip(res["res.partner"])
        elif isinstance(res, list):
            res = _strip(res)
        return res
