# -*- coding: utf-8 -*-
"""
res.partner extensions for Dutchie customer tracking.

Adds loyalty-id join key, provenance, and stub/review flags used by the
lane-watcher create-on-detect flow and the nightly merge cron.

Note: `x_dutchie_customer_id` (the primary strong-key) + its UNIQUE
constraint live on `mint_dutchie_sync` (this module depends on it —
see mint_pos_bridge/__manifest__.py). We do NOT redeclare the column
here to avoid a duplicate constraint on module upgrade.
"""
import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


PARTNER_ORIGINS = [
    ('odoo_manual', 'Odoo (manual)'),
    ('web_checkout', 'Web Checkout'),
    ('dutchie_weborder', 'Dutchie (Web Order)'),
    ('dutchie_walkin', 'Dutchie (Walk-in)'),
]


class ResPartner(models.Model):
    _inherit = 'res.partner'

    # Loyalty ID — secondary strong key when no customer_id is captured
    # (mint_dutchie_sync owns x_dutchie_customer_id).
    x_dutchie_loyalty_id = fields.Char(
        string='Dutchie Loyalty ID',
        index=True,
        copy=False,
    )

    # Provenance — which system created this partner record.
    x_partner_origin = fields.Selection(
        PARTNER_ORIGINS,
        string='Origin',
        default='odoo_manual',
        index=True,
    )

    # Stub = partner created with minimal info (typically anonymous walk-in
    # observed by the lane watcher before any identifying data arrives).
    # Cleared automatically once the partner has both a real name and at
    # least one of (phone, email, dutchie_customer_id).
    x_is_stub = fields.Boolean(
        string='Is Stub',
        default=False,
        index=True,
    )

    # Flagged by the nightly merge cron when a stub has ambiguous match
    # candidates (2+ strong-key matches, or only weak-key matches). These
    # require human review via POS Bridge → Customers → Needs Merge Review.
    x_merge_needs_review = fields.Boolean(
        string='Needs Merge Review',
        default=False,
        index=True,
    )

    # Audit trail.
    x_first_seen_at = fields.Datetime(string='First Seen')
    x_last_merged_at = fields.Datetime(string='Last Merged')

    def _recompute_stub_flag(self):
        """Clear x_is_stub when the partner has enough identifying data."""
        for partner in self:
            if not partner.x_is_stub:
                continue
            has_identity = bool(
                partner.x_dutchie_customer_id
                or partner.x_dutchie_loyalty_id
                or partner.phone
                or partner.email
            )
            has_name = bool(partner.name and not partner.name.startswith('Walk-in #'))
            if has_identity and has_name:
                partner.x_is_stub = False

    @api.model
    def _cron_clear_staff_customer_flags(self):
        """Self-healing guard: strip Dutchie/POS customer flags off staff.

        Staff who also shop (walk-in or web order) can get matched by
        phone/email and stamped with x_dutchie_customer_id or a Dutchie
        origin. Under the customer-isolation rule (mint_customer_api's
        partner_rule_hide_web_customers) that hides the employee's own
        contact from colleagues — which breaks ticket-follower rendering
        for internal users. The roster backfill (mint_dutchie_sync) and the
        POS bridge partner-upgrade path now skip staff at write time; this
        cron is the backstop that re-cleans anything that slips through from
        any path.

        Deliberately does NOT touch is_web_customer: that flag marks a
        genuine web signup and may legitimately belong to an employee who
        registered online, so clearing it is a human decision, not
        automatic. Staff = an employee work-contact, or a partner backing a
        non-share internal user.
        """
        Partner = self.env['res.partner'].sudo()
        staff_flagged = Partner.search([
            '&',
            '|', ('employee', '=', True), ('user_ids.share', '=', False),
            '|', ('x_partner_origin', 'in', ['dutchie_walkin', 'web_checkout']),
                 ('x_dutchie_customer_id', '!=', False),
        ])
        cleaned = 0
        for partner in staff_flagged:
            vals = {}
            if partner.x_dutchie_customer_id:
                vals['x_dutchie_customer_id'] = False
            if partner.x_partner_origin in ('dutchie_walkin', 'web_checkout'):
                vals['x_partner_origin'] = 'odoo_manual'
            if vals:
                partner.write(vals)
                cleaned += 1
                _logger.info(
                    "Staff-flag guard: cleared %s off staff partner %s (%s)",
                    list(vals), partner.id, partner.name)
        if cleaned:
            _logger.info("Staff-flag guard: cleaned %s staff partner(s)", cleaned)
        return cleaned
