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
from odoo import fields, models


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
