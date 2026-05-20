import logging

from odoo import api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class BulkRevokeByBrandWizard(models.TransientModel):
    _name = 'mint.deal.bulk.revoke.wizard'
    _description = 'Bulk-revoke every PTL deal for a brand within a date range'

    brand_id = fields.Many2one(
        'mint.brand',
        string='Brand',
        required=True,
    )
    date_start = fields.Date(string='Date Start', required=True)
    date_end = fields.Date(string='Date End', required=True)
    market_id = fields.Many2one(
        'mint.region',
        string='Market (optional)',
        help='Restrict to a single market. Leave empty to hit every market.',
    )
    brand_name_typed = fields.Char(
        string='Type the brand name to confirm',
        required=True,
        help='Must match the selected brand name exactly (case-sensitive). '
             'This is a deliberate friction step — bulk revoke is irreversible.',
    )
    reason = fields.Text(string='Reason', required=True)

    # ─── Preview ─────────────────────────────────────────────────────────

    matching_deal_ids = fields.Many2many(
        'mint.ptl.deal',
        compute='_compute_matching',
        string='Matching PTL Deals',
        readonly=True,
    )
    matching_day_ids = fields.Many2many(
        'mint.ptl.day',
        compute='_compute_matching',
        string='Matching PTL Days',
        readonly=True,
    )
    matching_deal_count = fields.Integer(
        compute='_compute_matching',
        string='# PTL Deals',
        readonly=True,
    )
    matching_day_count = fields.Integer(
        compute='_compute_matching',
        string='# PTL Days',
        readonly=True,
    )

    @api.depends('brand_id', 'date_start', 'date_end', 'market_id')
    def _compute_matching(self):
        for wiz in self:
            if not wiz.brand_id or not wiz.date_start or not wiz.date_end:
                wiz.matching_deal_ids = False
                wiz.matching_day_ids = False
                wiz.matching_deal_count = 0
                wiz.matching_day_count = 0
                continue
            day_domain = [
                ('date', '>=', wiz.date_start),
                ('date', '<=', wiz.date_end),
                ('deal_ids.brand_id', '=', wiz.brand_id.id),
            ]
            if wiz.market_id:
                day_domain.append(('market_id', '=', wiz.market_id.id))
            days = self.env['mint.ptl.day'].search(day_domain)
            deals = days.mapped('deal_ids').filtered(
                lambda d: d.brand_id.id == wiz.brand_id.id
            )
            wiz.matching_day_ids = [(6, 0, days.ids)]
            wiz.matching_deal_ids = [(6, 0, deals.ids)]
            wiz.matching_day_count = len(days)
            wiz.matching_deal_count = len(deals)

    # ─── Action ──────────────────────────────────────────────────────────

    def action_apply(self):
        self.ensure_one()
        if not self.reason or not self.reason.strip():
            raise UserError("Reason is required.")
        if self.brand_name_typed != self.brand_id.name:
            raise UserError(
                f"Typed brand name does not match. Expected exactly "
                f"\"{self.brand_id.name}\" (case-sensitive)."
            )
        if not self.matching_deal_ids:
            raise UserError(
                "No matching deals — nothing to revoke. "
                "Check date range and market filter."
            )

        RevokeWizard = self.env['mint.deal.revoke.wizard']
        bulk_reason = (
            f"[Bulk Revoke By Brand] {self.brand_id.name} "
            f"{self.date_start} → {self.date_end}"
            f"{' (' + self.market_id.code + ')' if self.market_id else ''}: "
            f"{self.reason}"
        )

        revoked = 0
        for deal in self.matching_deal_ids:
            wiz = RevokeWizard.create({
                'deal_id': deal.id,
                'scope': 'all_instances',
                'reason': bulk_reason,
            })
            try:
                wiz.action_apply()
                revoked += 1
            except Exception as e:
                _logger.warning(
                    'Bulk revoke: skipping deal %s — %s', deal.id, e
                )

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Bulk Revoke applied',
                'message': (
                    f"Revoked {revoked} / {len(self.matching_deal_ids)} "
                    f"deal(s) for brand \"{self.brand_id.name}\". "
                    f"Check the Revocation Audit Log for the per-deal trail."
                ),
                'type': 'success',
                'sticky': True,
            },
        }
