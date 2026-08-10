from odoo import api, fields, models
from odoo.exceptions import ValidationError


class MintDecisionVote(models.Model):
    _name = 'mint.decision.vote'
    _description = 'Decision Vote'
    _order = 'write_date desc'

    decision_id = fields.Many2one(
        'mint.decision', required=True, ondelete='cascade', index=True)
    option_id = fields.Many2one(
        'mint.decision.option', string='Choice', required=True,
        ondelete='restrict',
        domain="[('decision_id', '=', decision_id)]")
    voter_id = fields.Many2one(
        'res.users', string='Voter', required=True, index=True,
        ondelete='cascade', default=lambda self: self.env.user)
    voter_name = fields.Char(related='voter_id.name', store=True, string='Name')
    comment = fields.Text(string='Reasoning / Input')

    _sql_constraints = [
        ('decision_voter_uniq', 'unique(decision_id, voter_id)',
         'Each user can only vote once per decision.'),
    ]

    @api.constrains('option_id', 'decision_id')
    def _check_option_belongs_to_decision(self):
        for rec in self:
            if rec.option_id.decision_id != rec.decision_id:
                raise ValidationError('The chosen option does not belong to this decision.')

    @api.depends('voter_name', 'option_id.name')
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = ' — '.join(
                p for p in (rec.voter_name, rec.option_id.name) if p) or 'Vote'
