from odoo import _, api, exceptions, fields, models


class MintAmbassadorShift(models.Model):
    """A scheduled ambassador shift. Location is either a Mint store
    (`company_id`) or an external venue (`external_location` free text),
    selected via `location_type`. XOR constraint enforces exactly one is set."""

    _name = 'mint.ambassador.shift'
    _description = 'Ambassador Shift'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'shift_date desc, start_time'

    ambassador_id = fields.Many2one(
        'mint.ambassador',
        string='Ambassador',
        required=True,
        index=True,
        ondelete='cascade',
        tracking=True,
    )
    location_type = fields.Selection(
        selection=[
            ('mint_store', 'Mint Store'),
            ('partner', 'Partner'),
            ('competitor', 'Competitor'),
            ('event', 'Event'),
        ],
        string='Location Type',
        required=True,
        default='mint_store',
        tracking=True,
    )
    company_id = fields.Many2one(
        'res.company',
        string='Mint Store',
        help='Required when location_type is mint_store.',
        index=True,
    )
    external_location = fields.Char(
        string='External Location',
        help='Required when location_type is not mint_store.',
    )
    shift_date = fields.Date(
        string='Date',
        required=True,
        index=True,
        tracking=True,
    )
    start_time = fields.Float(
        string='Start',
        required=True,
        help='24h decimal — e.g. 9.5 = 9:30 AM.',
    )
    end_time = fields.Float(
        string='End',
        required=True,
    )
    state = fields.Selection(
        selection=[
            ('scheduled', 'Scheduled'),
            ('completed', 'Completed'),
            ('cancelled', 'Cancelled'),
            ('no_show', 'No Show'),
        ],
        string='Status',
        default='scheduled',
        required=True,
        tracking=True,
    )
    notes = fields.Text(string='Notes')

    brand_id = fields.Many2one(
        related='ambassador_id.brand_id',
        store=True,
        index=True,
    )
    name = fields.Char(
        string='Name',
        compute='_compute_name',
        store=True,
    )
    duration_hours = fields.Float(
        string='Hours',
        compute='_compute_duration',
        store=True,
    )
    start_datetime = fields.Datetime(
        string='Start (datetime)',
        compute='_compute_datetimes',
        store=True,
    )
    end_datetime = fields.Datetime(
        string='End (datetime)',
        compute='_compute_datetimes',
        store=True,
    )

    @api.depends('ambassador_id', 'shift_date', 'company_id', 'external_location')
    def _compute_name(self):
        for rec in self:
            who = rec.ambassador_id.name or '—'
            where = rec.company_id.display_name or rec.external_location or '—'
            when = rec.shift_date and rec.shift_date.isoformat() or '—'
            rec.name = f'{who} • {where} • {when}'

    @api.depends('start_time', 'end_time')
    def _compute_duration(self):
        for rec in self:
            if rec.start_time is not None and rec.end_time is not None:
                rec.duration_hours = max(rec.end_time - rec.start_time, 0.0)
            else:
                rec.duration_hours = 0.0

    @api.depends('shift_date', 'start_time', 'end_time')
    def _compute_datetimes(self):
        """Calendar view needs datetime endpoints. Treat times as the user's
        local company tz semantically; stored as naïve UTC for simplicity —
        good enough for scheduling display, refine later if cross-tz becomes
        a real concern."""
        from datetime import datetime, timedelta
        for rec in self:
            if not rec.shift_date:
                rec.start_datetime = False
                rec.end_datetime = False
                continue
            base = datetime.combine(rec.shift_date, datetime.min.time())
            rec.start_datetime = base + timedelta(hours=rec.start_time or 0)
            rec.end_datetime = base + timedelta(hours=rec.end_time or 0)

    @api.constrains('location_type', 'company_id', 'external_location')
    def _check_location_xor(self):
        for rec in self:
            if rec.location_type == 'mint_store':
                if not rec.company_id:
                    raise exceptions.ValidationError(
                        _('Pick a Mint store when Location Type is "Mint Store".'))
                if rec.external_location:
                    raise exceptions.ValidationError(
                        _('Clear the external location when Location Type is "Mint Store".'))
            else:
                if not rec.external_location:
                    raise exceptions.ValidationError(
                        _('Enter an external location for non-Mint-store shifts.'))
                if rec.company_id:
                    raise exceptions.ValidationError(
                        _('Clear the Mint store field for non-Mint-store shifts.'))

    @api.constrains('start_time', 'end_time')
    def _check_time_order(self):
        for rec in self:
            if rec.start_time is not None and rec.end_time is not None:
                if rec.end_time < rec.start_time:
                    raise exceptions.ValidationError(
                        _('End time must be on or after start time.'))

    def action_mark_completed(self):
        self.write({'state': 'completed'})

    def action_mark_no_show(self):
        self.write({'state': 'no_show'})

    def action_cancel(self):
        self.write({'state': 'cancelled'})

    def action_reset_scheduled(self):
        self.write({'state': 'scheduled'})
