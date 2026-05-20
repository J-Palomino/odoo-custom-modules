from odoo import fields, models


class MintBillboardScheduleItem(models.Model):
    """One line of a billboard schedule — pairs a creative with the boards it
    runs on for a specific time slot."""

    _name = 'mint.billboard.schedule.item'
    _description = 'Billboard Schedule Item'
    _order = 'schedule_id, sequence, id'

    schedule_id = fields.Many2one(
        'mint.billboard.schedule',
        string='Schedule',
        required=True,
        index=True,
        ondelete='cascade',
    )
    creative_id = fields.Many2one(
        'mint.billboard.creative',
        string='Creative',
    )
    board_ids = fields.Many2many(
        'mint.billboard.board',
        string='Boards',
    )
    time_slot = fields.Selection(
        selection=[
            ('all_day', 'All Day'),
            ('sunrise_to_9pm', 'Sunrise – 9pm'),
            ('9pm_to_11pm', '9pm – 11pm'),
        ],
        string='Time Slot',
        default='all_day',
        required=True,
    )
    run_percentage = fields.Integer(
        string='Run %',
        default=100,
    )
    location_override = fields.Char(string='Location Override')
    notes = fields.Text(string='Notes')
    sequence = fields.Integer(string='Sequence', default=10)

    market_id = fields.Many2one(
        related='schedule_id.market_id',
        store=True,
        index=True,
    )
