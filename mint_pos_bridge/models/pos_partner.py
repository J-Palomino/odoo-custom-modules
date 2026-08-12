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

    # Which partner this record appears to duplicate.
    #
    # Set by the receipt-based reconciliation: a partner's own purchases are
    # looked up in Dutchie's check-in report (128), which returns the real
    # customer id, and that id already belongs to a different partner. The
    # two records are the same human — one built by the roster sync (carries
    # the id), one spawned by the transaction importer when its name match
    # missed and it created a fresh partner instead.
    #
    # Measured 2026-08-11: 118,649 of 119,115 unlinked point-holders resolve
    # this way (99.6%), and 97.9% of those collide with an existing partner.
    #
    # A POINTER, not a merge — and deliberately NOT a write of
    # x_dutchie_customer_id onto this record. That column carries
    # UNIQUE(x_dutchie_customer_id) (res_partner_dutchie_customer_id_unique),
    # so stamping the id here would raise on 97.9% of the population. Even
    # without the constraint the importer looks that field up with limit=1,
    # so duplicating it would resurrect the arbitrary-pick bug that created
    # this mess. Under-merging is recoverable; mis-merging 118k records is not.
    x_duplicate_of_id = fields.Many2one(
        'res.partner',
        string='Duplicate Of',
        index=True,
        ondelete='set null',
        help='Partner this record appears to duplicate, identified from the '
             'Dutchie customer id behind its own receipts. Advisory only — '
             'nothing is merged automatically.',
    )
    x_duplicate_evidence = fields.Char(
        string='Duplicate Evidence',
        help='How the duplicate was identified (Dutchie customer id and the '
             'receipt it came from), so a reviewer can check the claim '
             'rather than trust it.',
    )

    # Audit trail.
    x_first_seen_at = fields.Datetime(string='First Seen')
    x_last_merged_at = fields.Datetime(string='Last Merged')

    @api.model
    def _flag_duplicate_partners(self, pairs, batch=500):
        """Flag partners as duplicates of another. Writes NO customer ids.

        :param pairs: iterable of (partner_id, canonical_id, evidence)
        :returns: {'flagged': n, 'skipped': n}

        Idempotent — rerunning over the same pairs rewrites the same values.
        Skips self-pairs, and skips any partner that already carries a login:
        a claimed account is not something a batch job should relabel as a
        duplicate behind the customer's back.
        """
        Partner = self.env['res.partner'].sudo()
        flagged = skipped = 0
        pairs = [p for p in pairs if p and p[0] and p[1] and p[0] != p[1]]
        for i in range(0, len(pairs), batch):
            chunk = pairs[i:i + batch]
            claimed = set(self.env['res.users'].sudo().search(
                [('partner_id', 'in', [c[0] for c in chunk])]
            ).mapped('partner_id').ids)
            for pid, canonical, evidence in chunk:
                if pid in claimed:
                    skipped += 1
                    continue
                Partner.browse(pid).with_context(
                    tracking_disable=True, mail_notrack=True,
                ).write({
                    'x_duplicate_of_id': canonical,
                    'x_duplicate_evidence': (evidence or '')[:255],
                    'x_merge_needs_review': True,
                })
                flagged += 1
            self.env.cr.commit()  # 118k records is not one transaction
            _logger.info('duplicate flagging: %d flagged, %d skipped', flagged, skipped)
        return {'flagged': flagged, 'skipped': skipped}

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
