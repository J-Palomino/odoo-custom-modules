# -*- coding: utf-8 -*-
"""
Dutchie purchase log — one record per transaction imported.
Prevents duplicate imports and provides a browsable purchase history.
Awards loyalty points atomically on create so points can never be skipped.
"""
import logging

from odoo import api, fields, models

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
        """
        records = super().create(vals_list)
        if not self.env['loyalty.card']._mint_awards_enabled():
            _logger.info(
                'Loyalty: automatic awarding is OFF (%s != legacy) — imported '
                '%d purchase(s) without minting points',
                self.env['loyalty.card']._mint_award_mode(), len(records),
            )
            return records
        program = self.env['loyalty.program'].sudo().search(
            [('program_type', '=', 'loyalty')], limit=1
        )
        if not program:
            _logger.warning('Loyalty: No active loyalty program found — points NOT awarded for %d purchases', len(records))
            return records

        LoyaltyCard = self.env['loyalty.card'].sudo()
        for rec in records:
            if not rec.partner_id or not rec.loyalty_points:
                continue
            # Internal/staff accounts don't earn loyalty points. Skip any
            # partner linked to an internal Odoo user (res.users.share == False).
            if any(not u.share for u in rec.partner_id.sudo().user_ids):
                _logger.info(
                    'Loyalty: skipped %d points for internal user %s (receipt %s)',
                    int(rec.loyalty_points), rec.partner_id.name, rec.receipt_no,
                )
                continue
            card = LoyaltyCard.search([
                ('partner_id', '=', rec.partner_id.id),
                ('program_id', '=', program.id),
            ], limit=1)
            source = 'dutchie purchase import (receipt %s)' % (rec.receipt_no or '?')
            if card:
                card.with_context(**{SOURCE_CTX: source}).write(
                    {'points': card.points + rec.loyalty_points}
                )
            else:
                LoyaltyCard.with_context(**{SOURCE_CTX: source}).create({
                    'partner_id': rec.partner_id.id,
                    'program_id': program.id,
                    'points': rec.loyalty_points,
                    'company_id': rec.company_id.id,
                })
            _logger.info(
                'Loyalty: +%d points for %s (receipt %s)',
                int(rec.loyalty_points), rec.partner_id.name, rec.receipt_no,
            )
        return records
