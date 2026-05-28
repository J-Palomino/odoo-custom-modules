# -*- coding: utf-8 -*-
"""
Per-store Dutchie swimlane (POS lane).

Each `mint.pos.lane` row mirrors one swimlane column in one store's Dutchie POS.
The Odoo POS kanban groups by `mint.pos.order.lane_id`, so each store sees only
the lanes Dutchie actually surfaces for it — including custom lanes (e.g. an
Employee lane that only Mesa / 75th-Ave / Bell Rd / Scottsdale have).

`dutchie_lane_name` is the verbatim Dutchie `TransactionStatus` string. Unique
per (company, lane name) — case-sensitive, since Dutchie itself is.

`category` groups lanes that mean the same thing across stores (e.g. all
"Sales Floor" / "SALES FLOOR" / "Sales Floor / Needs Code" map to category
`floor`). It also seeds `mint.pos.order.state` for back-compat: until Phase 5,
`state` remains the source-of-truth for terminal values and the dual-write
synchronizer in `mint.pos.order.write()` keeps `state` and `lane_id.category`
consistent for live orders.
"""
import logging

from odoo import api, fields, models
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)


# Categories surface in the same enum used by mint.pos.order.state for live
# (non-terminal) orders. Terminal states (completed/cancelled/picked_up/
# delivery_completed) intentionally do NOT appear here — Dutchie never
# surfaces them as lanes.
LANE_CATEGORIES = [
    ('lobby', 'Lobby'),
    ('online_orders', 'Online Orders'),
    ('sales_floor', 'Sales Floor'),
    ('processing', 'Processing'),
    ('pickup', 'Pick-Up'),
    ('deli_counter', 'Deli Counter'),
    ('credit_checkout', 'Credit Checkout'),
    ('delivery', 'Delivery'),
    ('ready_delivery', 'Ready For Delivery'),
    ('delivery_progress', 'Delivery In Progress'),
    ('employee', 'Employee'),
    ('unassigned', 'Unassigned'),
    ('other', 'Other'),
]

VALID_CATEGORIES = frozenset(c for c, _ in LANE_CATEGORIES)

# Order state (mint.pos.order.state) → nearest lane category.
# The state enum carries legacy/frontend values (placed/confirmed/preparing/
# ready) that aren't lane categories — map them to the nearest live lane.
# Terminal states (completed/cancelled/picked_up/delivery_completed) are
# intentionally absent: those orders carry NO lane (lane_id=NULL).
STATE_TO_CATEGORY = {
    'lobby': 'lobby',
    'online_orders': 'online_orders',
    'sales_floor': 'sales_floor',
    'processing': 'processing',
    'pickup': 'pickup',
    'deli_counter': 'deli_counter',
    'credit_checkout': 'credit_checkout',
    'delivery': 'delivery',
    'ready_delivery': 'ready_delivery',
    'delivery_progress': 'delivery_progress',
    'placed': 'online_orders',
    'confirmed': 'online_orders',
    'preparing': 'processing',
    'ready': 'pickup',
}

# Lane category → order state, for the reverse (lane_id write → mirror state).
# Only the 1:1 overlap is mapped. employee/unassigned/other have no order
# state, so an order moved into one of those lanes keeps its current state.
CATEGORY_TO_STATE = {
    'lobby': 'lobby',
    'online_orders': 'online_orders',
    'sales_floor': 'sales_floor',
    'processing': 'processing',
    'pickup': 'pickup',
    'deli_counter': 'deli_counter',
    'credit_checkout': 'credit_checkout',
    'delivery': 'delivery',
    'ready_delivery': 'ready_delivery',
    'delivery_progress': 'delivery_progress',
}


class MintPosLane(models.Model):
    _name = 'mint.pos.lane'
    _description = 'Mint POS Swimlane (per-store)'
    _order = 'company_id, sequence, id'

    name = fields.Char(
        string='Display Name',
        required=True,
        help='Human-readable lane name shown on the Odoo kanban column. '
             'Defaults to dutchie_lane_name on create.',
    )
    company_id = fields.Many2one(
        'res.company',
        string='Store',
        required=True,
        index=True,
        ondelete='cascade',
    )
    dutchie_lane_name = fields.Char(
        string='Dutchie TransactionStatus',
        required=True,
        index=True,
        help='Verbatim Dutchie TransactionStatus string. Case-sensitive — '
             'Dutchie distinguishes "Sales Floor" from "SALES FLOOR".',
    )
    category = fields.Selection(
        selection=LANE_CATEGORIES,
        string='Category',
        index=True,
        help='Cross-store grouping. Mirrors mint.pos.order.state values '
             '(non-terminal only). Leave blank for ops-triage queue.',
    )
    sequence = fields.Integer(string='Sequence', default=10, index=True)
    color = fields.Integer(string='Kanban Color', default=0)
    is_terminal = fields.Boolean(
        string='Terminal',
        default=False,
        help='Reserved. Dutchie does not surface terminal lanes today.',
    )
    push_notification_template_id = fields.Many2one(
        'mail.template',
        string='Push Notification Template',
        help='Optional per-lane override. When unset, push notifications fall '
             'back to the state-keyed templates in _get_notification_messages().',
    )
    active = fields.Boolean(string='Active', default=True)
    note = fields.Text(string='Ops Notes')

    _sql_constraints = [
        (
            'uniq_company_dutchie',
            'UNIQUE(company_id, dutchie_lane_name)',
            'Dutchie lane name must be unique per store.',
        ),
    ]

    @api.model
    def find_or_create_by_dutchie_name(self, company_id, dutchie_lane_name, name=None):
        """Find a lane by (company, dutchie name) or create one.

        Idempotent. The unique constraint protects against the race where two
        LaneWatcher pods create the same (company, name) row simultaneously —
        callers should catch IntegrityError and re-read.

        Returns the recordset for the lane.
        """
        if not company_id:
            raise ValidationError('company_id required')
        lane_name = (dutchie_lane_name or '').strip()
        if not lane_name:
            # Empty Dutchie status → bucket into a store-specific Unassigned lane
            # so the order is still visible. category=unassigned categorizes it.
            lane_name = 'Unassigned'
            name = name or 'Unassigned'

        lane = self.search(
            [('company_id', '=', company_id), ('dutchie_lane_name', '=', lane_name)],
            limit=1,
        )
        if lane:
            return lane

        return self.create({
            'company_id': company_id,
            'dutchie_lane_name': lane_name,
            'name': name or lane_name,
            # category left blank — ops triage queue surfaces uncategorized lanes
        })
