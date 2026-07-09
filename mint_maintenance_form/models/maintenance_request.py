import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class MaintenanceRequest(models.Model):
    _inherit = "maintenance.request"

    # Restrict maintenance assignees to INTERNAL users. Portal accounts
    # (web/customer logins, share=True) must never be offered as a Technician.
    # This field-level domain applies to every view (list/kanban/search) and the
    # activity picker; the form view further narrows it to the selected team's
    # members. Without this, base maintenance offered all res.users — including
    # the ~397 portal accounts — as assignable.
    user_id = fields.Many2one(domain="[('share', '=', False)]")

    x_intake_channel = fields.Selection(
        selection=[
            ("phone", "Phone"),
            ("sms", "SMS"),
            ("web", "Web Form"),
            ("manual", "Manual"),
        ],
        string="Intake Channel",
        index=True,
        help="How this ticket came in. Set automatically by the Phone-Operator "
        "(phone) and SMS-Manager (sms) agents on create, and by the website "
        "request forms (web). Left blank for tickets opened directly in Odoo "
        "unless explicitly set to 'manual'.",
    )

    @api.model
    def _cron_unfollow_customers_from_internal(self):
        """Monitor + self-heal: drop customer partners from maintenance followers.

        Maintenance requests route to internal teams (IT/Engineering/
        Facilities); a customer partner — hidden from internal users by the
        customer-isolation rule (mint_customer_api partner_rule_hide_web_customers)
        — has no business following one. When it does, any internal user
        opening the ticket hits an AccessError as the follower avatars render.
        That is exactly what blocked Pablo (user 5) on ~15 IT tickets. This
        cron finds such followers and unsubscribes them, logging each removal
        so the action is visible in the log stream.

        Scans ``mail.followers`` directly so the query touches only maintenance
        followers (a tiny set), never the full multi-million customer table.
        The sibling staff-flag guard (mint_pos_bridge) covers the *staff*
        mislabel; this covers *genuine* customers following internal tickets.
        """
        Followers = self.env['mail.followers'].sudo()
        bad = Followers.search([
            ('res_model', '=', 'maintenance.request'),
            '|', '|',
            ('partner_id.is_web_customer', '=', True),
            ('partner_id.x_partner_origin', 'in', ['dutchie_walkin', 'web_checkout']),
            ('partner_id.x_dutchie_customer_id', '!=', False),
        ])
        if not bad:
            return 0
        by_request = {}
        for follower in bad:
            by_request.setdefault(follower.res_id, set()).add(follower.partner_id.id)
        Request = self.env['maintenance.request'].sudo()
        removed = 0
        for res_id, partner_ids in by_request.items():
            request = Request.browse(res_id)
            if not request.exists():
                continue
            request.message_unsubscribe(partner_ids=list(partner_ids))
            removed += len(partner_ids)
            _logger.info(
                "Ticket-follower guard: unsubscribed customer partner(s) %s "
                "from maintenance.request %s (%s)",
                sorted(partner_ids), res_id, request.name)
        if removed:
            _logger.info(
                "Ticket-follower guard: removed %s customer follower(s) from "
                "%s maintenance ticket(s)", removed, len(by_request))
        return removed
