# -*- coding: utf-8 -*-
"""Loyalty balance recompute — the reconciliation formula, as code.

Between 2026-04-10 and 2026-07-10 points were multi-credited (the nightly
importer wrote loyalty.card AND the model awarded atomically; web pickup also
credited at create_order and mark_paid), leaving balances inflated ~1.68x
org-wide. All credit paths were fixed by 2026-07-10; the balances were
reconciled on 2026-07-27 (Odoo #106621).

That reconciliation ran as a one-off script, which meant the formula — and the
guards that make it safe — lived nowhere reviewable. This module encodes both
so the recompute is auditable, testable, and repeatable.

    correct = SUM(mint.dutchie.purchase.loyalty_points)   [join on partner_id]
            - SUM(mint.discount.redemption_points_cost)   [status != 'voided']

Guards, each of which prevented real damage in the live run:

  * NO GROUND TRUTH — a card whose partner has no purchase rows is skipped.
    There is no evidence its balance is wrong, and the bare formula would zero
    it. One real customer held 1,297 points from a non-purchase source and
    would have been wiped (and driven negative by a redemption).
  * NEVER GRANT — a card at or below its computed value is left alone. This
    recompute only ever removes inflation; it does not hand out points.
  * CLAMP — a computed value below zero is never written.

dry_run defaults to True: calling this by accident reports, it does not write.
"""
from odoo import api, models


class LoyaltyCard(models.Model):
    _inherit = 'loyalty.card'

    @api.model
    def _mint_earned_by_partner(self):
        """partner_id -> SUM(loyalty_points) from the purchase ledger."""
        earned = {}
        groups = self.env['mint.dutchie.purchase'].sudo().read_group(
            [], ['loyalty_points'], ['partner_id'], lazy=False)
        for row in groups:
            partner = row.get('partner_id')
            if partner:
                earned[partner[0]] = row.get('loyalty_points') or 0.0
        return earned

    @api.model
    def _mint_redeemed_by_partner(self):
        """partner_id -> SUM(points cost) of redemptions still in force.

        'voided' is excluded because action_void_redemption already refunds
        those points to the card; counting them would deduct twice.
        """
        redeemed = {}
        rows = self.env['mint.discount'].sudo().search_read(
            [('discount_type', '=', 'loyalty_redemption'),
             ('redemption_status', '!=', 'voided')],
            ['redemption_partner_id', 'redemption_points_cost'])
        for row in rows:
            partner = row.get('redemption_partner_id')
            if partner:
                redeemed[partner[0]] = (redeemed.get(partner[0], 0.0)
                                        + (row.get('redemption_points_cost') or 0))
        return redeemed

    def mint_recompute_balance(self, earned=None, redeemed=None):
        """Return the correct balance for this single card, or None.

        None means "no ground truth" — the partner has no purchase rows, so
        the balance cannot be shown to be wrong and must not be touched.
        """
        self.ensure_one()
        if not self.partner_id:
            return None
        if earned is None:
            earned = self._mint_earned_by_partner()
        if redeemed is None:
            redeemed = self._mint_redeemed_by_partner()
        gross = earned.get(self.partner_id.id, 0.0)
        if gross <= 0:
            return None
        return gross - redeemed.get(self.partner_id.id, 0.0)

    @api.model
    def mint_reconcile_balances(self, dry_run=True, limit=None):
        """Recompute inflated balances. Reports by default; writes only when
        dry_run is explicitly False.

        Returns {'inflated', 'corrected', 'skipped_no_ground_truth',
                 'skipped_not_inflated', 'skipped_negative', 'delta',
                 'changes': [(card_id, old, new), ...]}.
        """
        earned = self._mint_earned_by_partner()
        redeemed = self._mint_redeemed_by_partner()

        stats = {'inflated': 0, 'corrected': 0, 'skipped_no_ground_truth': 0,
                 'skipped_not_inflated': 0, 'skipped_negative': 0,
                 'delta': 0.0, 'changes': []}

        cards = self.sudo().search([], limit=limit)
        for card in cards:
            correct = card.mint_recompute_balance(earned=earned, redeemed=redeemed)
            if correct is None:
                stats['skipped_no_ground_truth'] += 1
                continue
            if correct < 0:
                stats['skipped_negative'] += 1
                continue
            current = card.points or 0.0
            # Only ever reduce: never grant points from a recompute.
            if correct >= current - 0.5:
                stats['skipped_not_inflated'] += 1
                continue
            stats['inflated'] += 1
            stats['delta'] += correct - current
            stats['changes'].append((card.id, current, correct))
            if not dry_run:
                card.points = correct
                stats['corrected'] += 1
        return stats
