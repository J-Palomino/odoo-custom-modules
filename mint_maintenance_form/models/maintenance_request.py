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

    # Idempotency key for web-form submissions, stamped by the Fix-It
    # controller. Hash of (submitter, team, company, description) as the user
    # supplied it.
    #
    # A key exists because the obvious approach — comparing `description`
    # directly — cannot work: description is an HTML field with sanitize=True,
    # so Odoo rewrites the markup on write and what comes back out is never
    # byte-identical to what was posted. An exact match on it silently never
    # fires, which is precisely how the earlier guard came to be inert.
    # Hashing the submitted payload sidesteps HTML normalisation entirely and
    # gives an indexed equality lookup.
    #
    # copy=False so duplicating a ticket in the UI does not carry the original
    # ticket's key onto the copy and make the next real submission look like a
    # duplicate.
    x_submission_key = fields.Char(
        string="Submission Key",
        index=True,
        copy=False,
        help="Idempotency key for tickets filed through the website request "
        "forms. Identical resubmissions within a short window resolve to the "
        "existing ticket instead of creating another one. Empty for tickets "
        "created directly in Odoo or by the phone/SMS agents.",
    )

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

    # Region rollup + store label, stamped from company_id by the
    # "Maintenance: set state/locale from company" automation (see
    # data/maintenance_region_data.xml). Previously Studio/manual fields; defined
    # here so they survive a DB restore and the stamping action never crashes on
    # a missing column. x_state_region is a FULL-name rollup used as the parent
    # sort dimension (Arizona/Nevada/.../Distribution/Corporate); company_id is
    # the child (per-store) dimension. Kept indexed for group-by performance.
    x_state_region = fields.Char(
        string="State",
        index=True,
        help="Regional rollup for sorting tickets: full US state name for "
        "store companies, 'Distribution' for the Warehouse-Ordering entities, "
        "'Corporate' for stateless corporate companies. Set automatically from "
        "company_id.",
    )
    x_store_location = fields.Char(
        string="Store Location",
        help="Human-readable store/company name, set automatically from "
        "company_id.",
    )

    # Cross-links between related tickets (shared root cause, compounding
    # severity, or one shared fix). Base maintenance ships no request-to-request
    # relation, so before this the only way to relate tickets was a chatter
    # comment — invisible to list views, filters and reporting.
    #
    # Name kept as the manual field created live 2026-07-28 (same approach as
    # x_sdlc_* and x_state_region above), so the module adopts the existing
    # column and its data rather than creating a second one.
    #
    # The explicit relation/column names are required, not cosmetic: this is a
    # self-referential m2m, and Odoo's default naming derives both columns from
    # the comodel, which collides when source and target are the same model.
    #
    # NOTE: a plain m2m is NOT symmetric — writing A→B does not create B→A.
    # Callers must set both sides. Kept deliberately simple rather than adding a
    # mirroring write() override, which would surprise anyone doing a bulk load.
    x_related_request_ids = fields.Many2many(
        comodel_name="maintenance.request",
        relation="maintenance_request_related_rel",
        column1="request_id",
        column2="related_request_id",
        string="Related Requests",
        help="Other maintenance requests related to this one (shared root "
        "cause, compounding severity, or a shared fix). Non-symmetric: adding "
        "B here does not add this record to B.",
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
