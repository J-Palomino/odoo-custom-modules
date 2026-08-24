# -*- coding: utf-8 -*-
"""
Dutchie purchase log — one record per transaction imported.
Prevents duplicate imports and provides a browsable purchase history.
Awards loyalty points atomically on create so points can never be skipped.
"""
import logging
from datetime import timedelta

from odoo import _, api, fields, models

from .loyalty_card import SOURCE_CTX

_logger = logging.getLogger(__name__)


class DutchiePurchase(models.Model):
    _name = 'mint.dutchie.purchase'
    _description = 'Dutchie Purchase Record'
    _order = 'date desc, id desc'
    _rec_name = 'receipt_no'

    partner_id = fields.Many2one(
        'res.partner',
        string='Customer',
        required=True,
        ondelete='cascade',
        index=True,
    )
    company_id = fields.Many2one(
        'res.company',
        string='Store',
        required=True,
        index=True,
    )

    # Transaction identifiers
    receipt_no = fields.Char(string='Receipt #', index=True)
    date = fields.Date(string='Date', required=True, index=True)

    # Financials
    gross_total = fields.Float(string='Gross Total', digits=(12, 2))
    discount = fields.Float(string='Discount', digits=(12, 2))
    net_total = fields.Float(string='Net Total', digits=(12, 2))
    tax = fields.Float(string='Tax', digits=(12, 2))

    # Points awarded
    loyalty_points = fields.Float(string='Points Awarded', digits=(12, 2))

    # Line items stored as text (JSON) for reference without needing a third model
    line_items_json = fields.Text(
        string='Line Items (JSON)',
        help='JSON array of {product, sku, qty, price} from Dutchie dispensations report.',
    )

    _receipt_unique = models.Constraint(
        'UNIQUE(receipt_no, company_id)',
        'Receipt number must be unique per store.',
    )

    @api.model_create_multi
    def create(self, vals_list):
        """Award loyalty points atomically when purchases are created.

        Gated by ``mint.loyalty.award_mode`` (see ``loyalty_card.py``). The
        flat 1 pt/$1 computed here has never agreed with Dutchie, which
        accrues ~0.8 pt/$1 and only for customers who opted in — so awarding
        it fabricates balances the register will not honour. Purchases are
        still imported in full; only the point award is suppressed.

        A region listed in ``mint.loyalty.region_award_rates`` mints at its
        own rate even while the global mode is ``off``. Michigan runs a
        cash-value program (0.025 pt/$ net, matching Dutchie LSP 576's
        accrual), so its balances track the register instead of the retired
        1 pt/$1 scheme.
        """
        records = super().create(vals_list)
        records._grant_spin_tickets()

        LoyaltyCard = self.env['loyalty.card'].sudo()
        legacy = LoyaltyCard._mint_awards_enabled()

        # Resolve each purchase to the points it should mint. Legacy mode
        # replays the imported 1 pt/$1 figure; otherwise only a region with
        # a configured rate mints, computed from net_total. Regional awards
        # deliberately do NOT skip internal users: those programs cover
        # employees too (per Juan, 2026-08-18 — "michigan employees should
        # be able to accrue and spend points").
        awards = []
        for rec in records:
            if not rec.partner_id:
                continue
            if legacy:
                points = rec.loyalty_points
                if not points:
                    continue
                # Internal/staff accounts don't earn under the legacy program.
                # Skip any partner linked to an internal Odoo user
                # (res.users.share == False).
                if any(not u.share for u in rec.partner_id.sudo().user_ids):
                    _logger.info(
                        'Loyalty: skipped %d points for internal user %s (receipt %s)',
                        int(points), rec.partner_id.name, rec.receipt_no,
                    )
                    continue
            else:
                rate = LoyaltyCard._mint_region_award_rate(rec.company_id)
                if not rate:
                    continue
                # Negative net (a return) mints negative points — the
                # deduction mirrors the accrual.
                points = round((rec.net_total or 0.0) * rate, 2)
                if not points:
                    continue
            awards.append((rec, points))

        if not awards:
            if not legacy:
                _logger.info(
                    'Loyalty: automatic awarding is OFF (%s != legacy) and no '
                    'regional rate applied — imported %d purchase(s) without '
                    'minting points',
                    LoyaltyCard._mint_award_mode(), len(records),
                )
            return records

        program = self.env['loyalty.program'].sudo().search(
            [('program_type', '=', 'loyalty')], limit=1
        )
        if not program:
            _logger.warning('Loyalty: No active loyalty program found — points NOT awarded for %d purchases', len(records))
            return records

        for rec, points in awards:
            card = LoyaltyCard.search([
                ('partner_id', '=', rec.partner_id.id),
                ('program_id', '=', program.id),
            ], limit=1)
            source = 'dutchie purchase import (receipt %s)' % (rec.receipt_no or '?')
            if card:
                card.with_context(**{SOURCE_CTX: source}).write(
                    {'points': card.points + points}
                )
            else:
                LoyaltyCard.with_context(**{SOURCE_CTX: source}).create({
                    'partner_id': rec.partner_id.id,
                    'program_id': program.id,
                    'points': points,
                    'company_id': rec.company_id.id,
                })
            _logger.info(
                'Loyalty: %+.2f points for %s (receipt %s)',
                points, rec.partner_id.name, rec.receipt_no,
            )
        return records

    def _grant_spin_tickets(self):
        """Give the customer spin tickets for a completed purchase.

        ── Off by default, and deliberately hard to switch on wide ────────────
        Granting a ticket for a purchase is what turns spin-to-win into
        purchase -> chance -> prize. It is gated behind mint.config key
        `spin.purchase_grant` (see mint.spin.ticket.purchase_grant_settings)
        and ships disabled.

        ── Why max_age_days matters more than it looks ────────────────────────
        This importer runs in bulk and has been observed days behind. Enabling
        grants without an age limit means the next catch-up run mints a ticket
        for every backdated receipt at once — a silent, unbudgeted giveaway.
        The default window is 3 days: a customer who shopped today still gets
        their spin, a six-month backfill grants nothing.

        Failures never raise. The purchase import is the system of record for
        revenue; a promotion must not be able to block it.
        """
        # mint_api_v2 is not in this module's depends (the sync must install
        # without it), so probe the registry the same way the redemption push
        # does elsewhere in this codebase.
        if 'mint.spin.ticket' not in self.env:
            return

        Ticket = self.env['mint.spin.ticket'].sudo()
        try:
            settings = Ticket.purchase_grant_settings()
        except Exception:
            _logger.exception('spin: could not read purchase grant settings')
            return

        if not settings.get('enabled'):
            return

        per_purchase = int(settings.get('tickets') or 1)
        min_spend = float(settings.get('min_spend') or 0.0)
        max_age_days = int(settings.get('max_age_days') or 0)
        cutoff = (fields.Date.context_today(self)
                  - timedelta(days=max_age_days)) if max_age_days else None

        for rec in self:
            if not rec.partner_id:
                continue
            # A return (negative net) must not buy a spin.
            if (rec.net_total or 0.0) < max(min_spend, 0.01):
                continue
            if cutoff and rec.date and rec.date < cutoff:
                continue
            # Receipts are unique per store (see _receipt_unique), so this is a
            # stable idempotency key across re-imports.
            ref = 'purchase:%s:%s' % (rec.company_id.id, rec.receipt_no)
            try:
                Ticket.grant_for_ref(
                    partner=rec.partner_id,
                    source_ref=ref,
                    count=per_purchase,
                    source='purchase',
                    note=_('Purchase %s') % (rec.receipt_no or ''),
                )
            except Exception:
                # Never let a promotion break the revenue import.
                _logger.exception('spin: failed granting tickets for receipt %s',
                                  rec.receipt_no)
