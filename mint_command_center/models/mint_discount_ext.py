import logging
from datetime import timedelta

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class MintDiscountPTL(models.Model):
    """Extend mint.discount with PTL-specific fields and lifecycle cron."""
    _inherit = 'mint.discount'

    # Day-of-week availability (computed from PTL day associations)
    monday = fields.Boolean(string="Monday", default=False)
    tuesday = fields.Boolean(string="Tuesday", default=False)
    wednesday = fields.Boolean(string="Wednesday", default=False)
    thursday = fields.Boolean(string="Thursday", default=False)
    friday = fields.Boolean(string="Friday", default=False)
    saturday = fields.Boolean(string="Saturday", default=False)
    sunday = fields.Boolean(string="Sunday", default=False)

    # Source tracking
    source = fields.Selection([
        ('ptl', 'PTL'),
        ('dutchie', 'Dutchie POS'),
        ('manual', 'Manual'),
    ], string="Source", default='manual')

    # Link to PTL deal template
    ptl_deal_id = fields.Many2one(
        'mint.ptl.deal',
        string="PTL Deal",
        ondelete='set null',
    )

    DAY_NAME_MAP = {
        0: 'monday', 1: 'tuesday', 2: 'wednesday', 3: 'thursday',
        4: 'friday', 5: 'saturday', 6: 'sunday',
    }

    @api.model
    def _cron_ptl_daily_lifecycle(self):
        """Daily lifecycle for PTL discounts: activate, expire, recompute days, push."""
        today = fields.Date.today()

        # Activate deals where valid_from <= today
        to_activate = self.search([
            ('source', '=', 'ptl'),
            ('is_active', '=', False),
            ('valid_from', '<=', today),
            '|', ('valid_until', '=', False), ('valid_until', '>=', today),
        ])
        if to_activate:
            to_activate.write({'is_active': True})
            _logger.info('PTL lifecycle: activated %d discounts', len(to_activate))

        # Expire deals where valid_until < today
        to_expire = self.search([
            ('source', '=', 'ptl'),
            ('is_active', '=', True),
            ('valid_until', '!=', False),
            ('valid_until', '<', today),
        ])
        if to_expire:
            to_expire.write({'is_active': False})
            _logger.info('PTL lifecycle: expired %d discounts', len(to_expire))

        # Recompute day-of-week booleans for active PTL discounts
        active_ptl = self.search([
            ('source', '=', 'ptl'),
            ('is_active', '=', True),
            ('ptl_deal_id', '!=', False),
        ])
        for discount in active_ptl:
            self._recompute_day_booleans(discount)

        # Push all changes to inventory service
        changed = to_activate | to_expire | active_ptl
        if changed:
            self.env['mint.ptl.day']._push_discounts_to_redis(changed.ids)

        _logger.info('PTL lifecycle cron: %d activated, %d expired, %d recomputed',
                      len(to_activate), len(to_expire), len(active_ptl))

    def _recompute_day_booleans(self, discount):
        """Recompute monday-sunday booleans from linked PTL day dates."""
        deal = discount.ptl_deal_id
        if not deal:
            return

        today = fields.Date.today()
        future_cutoff = today + timedelta(days=60)

        days = self.env['mint.ptl.day'].search([
            ('deal_ids', 'in', deal.id),
            ('state', '=', 'published'),
            ('date', '>=', today),
            ('date', '<=', future_cutoff),
        ])

        day_bools = {name: False for name in self.DAY_NAME_MAP.values()}
        for day_rec in days:
            weekday_num = day_rec.date.weekday()
            day_bools[self.DAY_NAME_MAP[weekday_num]] = True

        discount.write(day_bools)
