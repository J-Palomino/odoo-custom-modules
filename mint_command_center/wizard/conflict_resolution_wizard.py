"""
Conflict Resolution wizard for mint.brand.calendar.entry → mint.ptl.day plot.

Before action_add_to_ptl writes any deal_ids, we look for existing deals on
the same (date × market) where (brand_id, product_category) matches the
entry's deal. If any overlap is found, the operator is shown this wizard
with per-conflict REPLACE / SKIP / KEEP_BOTH choices plus header-level
bulk apply.

REPLACE — existing deal is unlinked from the day; new entry plots in its
          place. Existing deal's mint.discount is NOT deactivated (other
          days may still reference it); only the day-link is severed.
SKIP    — new entry is NOT plotted on that date. Its state is bumped to
          'confirmed' so it stops sitting in 'tentative' but no deal_ids
          row is created.
KEEP_BOTH — both deals coexist on the day. No mutation to the existing
          deal; new entry plots normally.

Non-conflicting entries in the same batch plot normally with no wizard
involvement (the wizard's action_apply re-runs through the same
eligibility-and-plot loop but with bypass_conflict_check=True).
"""

from odoo import api, fields, models
from odoo.exceptions import UserError


RESOLUTION_SELECTION = [
    ('replace', 'Replace (unlink existing, plot new)'),
    ('skip', 'Skip (do not plot)'),
    ('keep_both', 'Keep Both (coexist on this day)'),
]


class ConflictResolutionWizard(models.TransientModel):
    _name = 'mint.ptl.conflict.resolution.wizard'
    _description = 'Resolve PTL day overlaps (brand × category) before plotting'

    entry_ids = fields.Many2many(
        'mint.brand.calendar.entry',
        relation='mint_ptl_conflict_entry_rel',
        string='Entries in this Batch',
        readonly=True,
    )
    conflict_line_ids = fields.One2many(
        'mint.ptl.conflict.line',
        'wizard_id',
        string='Conflicts',
    )
    bulk_apply = fields.Selection(
        selection=[
            ('none', '— per-row choice —'),
            ('replace_all', 'Replace All'),
            ('skip_all', 'Skip All'),
            ('keep_both_all', 'Keep Both (All)'),
        ],
        string='Bulk Apply',
        default='none',
        help='Convenience: set every line\'s resolution at once.',
    )
    non_conflict_count = fields.Integer(
        string='# Non-Conflicting Entries (will plot normally)',
        readonly=True,
    )

    @api.onchange('bulk_apply')
    def _onchange_bulk_apply(self):
        mapping = {
            'replace_all': 'replace',
            'skip_all': 'skip',
            'keep_both_all': 'keep_both',
        }
        target = mapping.get(self.bulk_apply)
        if target:
            for line in self.conflict_line_ids:
                line.resolution = target

    def action_apply(self):
        self.ensure_one()
        # Validate every line has a resolution.
        for line in self.conflict_line_ids:
            if not line.resolution:
                raise UserError(
                    "Every conflict needs a resolution. Pick one per row or "
                    "use Bulk Apply."
                )

        Day = self.env['mint.ptl.day']

        # Build a quick lookup: (entry_id) → resolution + existing_deal
        entry_resolution = {
            line.entry_id.id: (line.resolution, line.existing_deal_id)
            for line in self.conflict_line_ids
        }

        # Re-run the same eligibility filter the original method uses, then
        # branch per-entry on whether it had a resolved conflict.
        eligible = self.entry_ids.filtered(
            lambda e: e.deal_id and e.state in ('tentative', 'confirmed', 'approved')
        )

        for entry in eligible:
            decision, existing = entry_resolution.get(entry.id, ('keep_both', None))
            if decision == 'skip':
                # Operator chose not to plot — bump the entry state to
                # 'confirmed' so it stops sitting in 'tentative', no
                # deal_ids mutation.
                if entry.state == 'tentative':
                    entry.write({'state': 'confirmed'})
                continue

            day = Day.search([
                ('date', '=', entry.date),
                ('market_id', '=', entry.market_id.id),
            ], limit=1)
            if not day:
                day = Day.create({
                    'date': entry.date,
                    'market_id': entry.market_id.id,
                })

            if decision == 'replace' and existing:
                # Unlink the existing deal from this day, then plot ours.
                day.write({
                    'deal_ids': [(3, existing.id), (4, entry.deal_id.id)],
                })
            else:
                # keep_both, or no-conflict path
                day.write({'deal_ids': [(4, entry.deal_id.id)]})

            entry.write({
                'ptl_day_id': day.id,
                'state': 'confirmed' if entry.state == 'tentative' else entry.state,
            })

        # Audit summary on each affected day's chatter.
        affected_day_ids = list(set(
            self.conflict_line_ids.mapped('day_id').ids
        ))
        for day_id in affected_day_ids:
            day = Day.browse(day_id)
            day.message_post(
                body=(
                    f"Conflict resolution applied by "
                    f"<b>{self.env.user.name}</b>: "
                    f"{len(self.conflict_line_ids.filtered(lambda l: l.day_id.id == day_id))} "
                    f"overlap(s) on this day."
                ),
                message_type='comment',
            )

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Plotted with conflict resolution',
                'message': (
                    f"{len(eligible)} entries processed "
                    f"({len(self.conflict_line_ids)} conflicts resolved, "
                    f"{len(eligible) - len(self.conflict_line_ids)} plotted "
                    f"normally)."
                ),
                'type': 'success',
                'sticky': False,
            },
        }


class ConflictLine(models.TransientModel):
    _name = 'mint.ptl.conflict.line'
    _description = 'One overlap row in the Conflict Resolution wizard'
    _order = 'date, id'

    wizard_id = fields.Many2one(
        'mint.ptl.conflict.resolution.wizard',
        ondelete='cascade',
        required=True,
    )
    entry_id = fields.Many2one(
        'mint.brand.calendar.entry',
        string='Proposed Entry',
        required=True,
        readonly=True,
    )
    day_id = fields.Many2one(
        'mint.ptl.day',
        string='PTL Day',
        required=True,
        readonly=True,
    )
    date = fields.Date(
        related='day_id.date',
        readonly=True,
    )
    existing_deal_id = fields.Many2one(
        'mint.ptl.deal',
        string='Existing Deal',
        readonly=True,
    )
    proposed_deal_id = fields.Many2one(
        'mint.ptl.deal',
        related='entry_id.deal_id',
        readonly=True,
    )
    brand_id = fields.Many2one(
        'mint.brand',
        related='entry_id.brand_id',
        readonly=True,
    )
    resolution = fields.Selection(
        selection=RESOLUTION_SELECTION,
        string='Resolution',
        default='replace',
        required=True,
    )
