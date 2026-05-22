from odoo import api, fields, models
from odoo.exceptions import UserError


class MintAmbassador(models.Model):
    """A brand ambassador — external contractor representing a vendor at Mint
    stores, partner events, or competitor scouting. Backed by `res.partner`
    so name/email/phone live in one place and can participate in mail, tags,
    duplicate detection, etc."""

    _name = 'mint.ambassador'
    _description = 'Brand Ambassador'
    _inherit = ['mail.thread']
    _order = 'state, partner_id'

    partner_id = fields.Many2one(
        'res.partner',
        string='Person',
        required=True,
        index=True,
        ondelete='restrict',
        help='res.partner record carrying name, email, phone.',
        tracking=True,
    )
    brand_id = fields.Many2one(
        'mint.brand',
        string='Brand',
        index=True,
        tracking=True,
    )
    region_ids = fields.Many2many(
        'mint.region',
        'mint_ambassador_region_rel',
        'ambassador_id',
        'region_id',
        string='Markets',
    )
    state = fields.Selection(
        selection=[
            ('active', 'Active'),
            ('inactive', 'Inactive'),
        ],
        string='Status',
        default='active',
        required=True,
        tracking=True,
    )
    notes = fields.Text(string='Notes')

    shift_ids = fields.One2many(
        'mint.ambassador.shift',
        'ambassador_id',
        string='Shifts',
    )
    # `upcoming_shift_count` uses search= (not store=True) because the
    # compute depends on `today`, which a stored value can't track — at
    # midnight a stored count would silently include yesterday's shifts
    # until shift_ids is next written. `_search_upcoming_shift_count`
    # queries mint.ambassador.shift directly so the "Has Upcoming Shifts"
    # filter is always time-correct.
    upcoming_shift_count = fields.Integer(
        string='Upcoming Shifts',
        compute='_compute_shift_counts',
        search='_search_upcoming_shift_count',
    )
    total_shift_count = fields.Integer(
        string='Total Shifts',
        compute='_compute_shift_counts',
        store=True,
    )

    name = fields.Char(
        string='Name',
        related='partner_id.display_name',
        store=True,
        index=True,
    )

    @api.depends('shift_ids', 'shift_ids.shift_date', 'shift_ids.state')
    def _compute_shift_counts(self):
        today = fields.Date.context_today(self)
        for rec in self:
            rec.total_shift_count = len(rec.shift_ids)
            rec.upcoming_shift_count = len(rec.shift_ids.filtered(
                lambda s: s.shift_date and s.shift_date >= today
                and s.state == 'scheduled'
            ))

    def _search_upcoming_shift_count(self, operator, value):
        """Time-correct domain rewrite for `upcoming_shift_count`.

        Without this, the search filter would either reject (no store=True)
        or return stale results at midnight (with store=True, the cached
        value wouldn't drop yesterday's shifts until shift_ids is next
        written). We query mint.ambassador.shift directly so today's view
        of "upcoming" is always honored.

        Supports the operators the current views actually use:
          - "has any upcoming"  → > 0, != 0, >= 1
          - "has none upcoming" → = 0
        Other operators raise so a future view writer hits a clear error
        instead of silently incorrect filtering.
        """
        today = fields.Date.context_today(self)
        with_upcoming = self.env['mint.ambassador.shift'].search([
            ('shift_date', '>=', today),
            ('state', '=', 'scheduled'),
        ]).ambassador_id

        if (operator in ('>', '!=') and value == 0) or \
           (operator == '>=' and value == 1):
            return [('id', 'in', with_upcoming.ids)]
        if operator == '=' and value == 0:
            return [('id', 'not in', with_upcoming.ids)]
        raise UserError(
            f"_search_upcoming_shift_count: unsupported ({operator!r}, "
            f"{value!r}); add a branch here if a view needs it."
        )

    def action_view_shifts(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': f'Shifts — {self.name}',
            'res_model': 'mint.ambassador.shift',
            'view_mode': 'calendar,list,form',
            'domain': [('ambassador_id', '=', self.id)],
            'context': {'default_ambassador_id': self.id},
        }
