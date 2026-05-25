from odoo import _, api, exceptions, fields, models


class MintBillboardSchedule(models.Model):
    """A weekly billboard schedule for a market. Items pair creatives with
    boards + time slots. State machine: draft → sent → confirmed."""

    _name = 'mint.billboard.schedule'
    _description = 'Billboard Schedule'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'week_start_date desc, market_id'

    name = fields.Char(string='Name', compute='_compute_name', store=True)
    market_id = fields.Many2one(
        'mint.region',
        string='Market',
        required=True,
        index=True,
        tracking=True,
    )
    week_start_date = fields.Date(
        string='Week Of',
        required=True,
        index=True,
        tracking=True,
        help='Monday of the week this schedule covers.',
    )
    notes = fields.Text(string='Notes')
    state = fields.Selection(
        selection=[
            ('draft', 'Draft'),
            ('sent', 'Sent'),
            ('confirmed', 'Confirmed'),
        ],
        string='Status',
        default='draft',
        required=True,
        tracking=True,
    )
    sent_at = fields.Datetime(string='Sent At', readonly=True)

    item_ids = fields.One2many(
        'mint.billboard.schedule.item',
        'schedule_id',
        string='Items',
        copy=True,
    )
    item_count = fields.Integer(
        string='Items',
        compute='_compute_item_count',
    )

    _sql_constraints = [
        ('schedule_per_market_week_unique',
         'unique(market_id, week_start_date)',
         'One schedule per market per week.'),
    ]

    @api.depends('market_id', 'week_start_date')
    def _compute_name(self):
        for rec in self:
            mkt = rec.market_id.name or '—'
            wk = rec.week_start_date and rec.week_start_date.isoformat() or '—'
            rec.name = f'{mkt} • week of {wk}'

    @api.depends('item_ids')
    def _compute_item_count(self):
        for rec in self:
            rec.item_count = len(rec.item_ids)

    def action_mark_sent(self):
        for rec in self:
            rec.write({'state': 'sent', 'sent_at': fields.Datetime.now()})
        return True

    def action_mark_confirmed(self):
        for rec in self:
            rec.state = 'confirmed'
        return True

    def action_reset_draft(self):
        for rec in self:
            rec.write({'state': 'draft', 'sent_at': False})
        return True

    def action_duplicate_to_next_week(self):
        """Clone schedule (and items) to the following week. Mirrors the
        React app's `duplicateBillboardSchedule`."""
        self.ensure_one()
        if not self.week_start_date:
            raise exceptions.UserError(_('Set a week start date before duplicating.'))
        from datetime import timedelta
        new_week = self.week_start_date + timedelta(days=7)
        existing = self.search([
            ('market_id', '=', self.market_id.id),
            ('week_start_date', '=', new_week),
        ], limit=1)
        if existing:
            raise exceptions.UserError(
                _('Schedule for %(market)s week of %(week)s already exists.') % {
                    'market': self.market_id.name,
                    'week': new_week,
                }
            )
        clone = self.copy({
            'week_start_date': new_week,
            'state': 'draft',
            'sent_at': False,
            'notes': self.notes,
        })
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'mint.billboard.schedule',
            'res_id': clone.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_email_to_vendors(self):
        """Open the standard mail composer prefilled with the schedule
        summary. Replaces React's BillboardEmailModal mailto: link."""
        self.ensure_one()
        template = self.env.ref(
            'mint_command_center.mail_template_billboard_schedule',
            raise_if_not_found=False,
        )
        ctx = {
            'default_model': 'mint.billboard.schedule',
            'default_res_ids': [self.id],
            'default_composition_mode': 'comment',
            'default_use_template': bool(template),
            'default_template_id': template.id if template else False,
        }
        return {
            'name': _('Send Schedule'),
            'type': 'ir.actions.act_window',
            'res_model': 'mail.compose.message',
            'view_mode': 'form',
            'target': 'new',
            'context': ctx,
        }
