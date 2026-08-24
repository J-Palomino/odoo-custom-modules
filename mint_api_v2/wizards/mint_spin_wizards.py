# -*- coding: utf-8 -*-
"""Operator wizards for spin-to-win.

Seeding the prize pool and handing out tickets were both model methods only,
which meant an engineer with shell access was the only way to run a promotion.
These put both behind the Odoo UI.
"""
from odoo import _, api, fields, models
from odoo.exceptions import UserError


class MintSpinSeedWizard(models.TransientModel):
    _name = 'mint.spin.seed.wizard'
    _description = 'Seed the Spin-to-Win Prize Pool'

    batch = fields.Char(
        required=True,
        help='Label for this run, e.g. "launch-2026-09". Lets you count, '
             'pause or void a campaign as a unit.',
    )
    line_ids = fields.One2many(
        'mint.spin.seed.wizard.line', 'wizard_id', string='Prizes',
        help='How many of each discount to create. The mix IS the odds: 10 '
             'entries of 30% in a batch of 1000 makes it a 1-in-100 draw AND '
             'caps it at ten.',
    )
    total_entries = fields.Integer(compute='_compute_totals')
    long_odds = fields.Char(compute='_compute_totals', string='Rarest Prize')

    @api.depends('line_ids.count', 'line_ids.percent')
    def _compute_totals(self):
        for wiz in self:
            lines = wiz.line_ids.filtered(lambda l: l.count > 0)
            total = sum(lines.mapped('count'))
            wiz.total_entries = total
            rarest = min(lines, key=lambda l: l.count, default=None)
            wiz.long_odds = (
                _('%s%% off — %s of %s (1 in %s)') % (
                    rarest.percent, rarest.count, total,
                    round(total / rarest.count) if rarest.count else 0)
                if rarest and total else ''
            )

    def action_seed(self):
        """Create the batch, then show it."""
        self.ensure_one()
        distribution = {}
        for line in self.line_ids:
            if line.count <= 0:
                continue
            # Summing rather than overwriting: two lines of the same percent is
            # a reasonable thing for an operator to type.
            distribution[line.percent] = distribution.get(line.percent, 0) + line.count
        if not distribution:
            raise UserError(_("Add at least one prize with a count above zero."))

        self.env['mint.spin.prize'].seed_pool(distribution, self.batch)
        return {
            'type': 'ir.actions.act_window',
            'name': _('Prize Pool — %s') % self.batch,
            'res_model': 'mint.spin.prize',
            'view_mode': 'list,form',
            'domain': [('batch', '=', self.batch)],
        }


class MintSpinSeedWizardLine(models.TransientModel):
    _name = 'mint.spin.seed.wizard.line'
    _description = 'Spin Pool Seed Line'

    wizard_id = fields.Many2one('mint.spin.seed.wizard', required=True, ondelete='cascade')
    percent = fields.Integer(string='Percent Off', required=True)
    count = fields.Integer(string='How Many', required=True, default=0)


class MintSpinGrantWizard(models.TransientModel):
    _name = 'mint.spin.grant.wizard'
    _description = 'Grant Spin Tickets'

    partner_ids = fields.Many2many('res.partner', string='Customers', required=True)
    count = fields.Integer(string='Tickets Each', required=True, default=1)
    source = fields.Selection(
        [('grant', 'Staff Grant'),
         ('signup', 'Signup Bonus'),
         ('promo', 'Promotion'),
         ('points', 'Loyalty Points'),
         ('purchase', 'Purchase')],
        default='grant', required=True,
        help='WHERE a ticket came from decides what this promotion legally is. '
             'Purchase-sourced tickets make it purchase -> chance -> prize; '
             'get sign-off before using that.',
    )
    expires_at = fields.Datetime(string='Expires At')
    batch = fields.Char()
    note = fields.Char()

    def action_grant(self):
        self.ensure_one()
        if self.count < 1:
            raise UserError(_("Grant at least one ticket."))
        Ticket = self.env['mint.spin.ticket']
        for partner in self.partner_ids:
            Ticket.grant(
                partner=partner, count=self.count, source=self.source,
                expires_at=self.expires_at, batch=self.batch, note=self.note,
            )
        return {
            'type': 'ir.actions.act_window',
            'name': _('Spin Tickets'),
            'res_model': 'mint.spin.ticket',
            'view_mode': 'list,form',
            'domain': [('partner_id', 'in', self.partner_ids.ids)],
        }
