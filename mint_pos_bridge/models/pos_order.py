# -*- coding: utf-8 -*-
import json
import logging
import threading
from datetime import timedelta

from odoo import api, fields, models

from .pos_lane import CATEGORY_TO_STATE, STATE_TO_CATEGORY

_logger = logging.getLogger(__name__)

ORDER_STATES = [
    # Dutchie POS swimlane states
    ('lobby', 'Lobby'),
    ('online_orders', 'Online Orders'),
    ('sales_floor', 'Sales Floor / Needs Code'),
    ('processing', 'Processing'),
    ('pickup', 'Pick-Up'),
    ('deli_counter', 'Deli Counter'),
    ('credit_checkout', 'Credit Checkout'),
    ('delivery', 'Delivery'),
    ('ready_delivery', 'Ready For Delivery'),
    ('delivery_progress', 'Delivery In Progress'),
    ('delivery_completed', 'Delivery Completed'),
    # Legacy / frontend states (mapped from checkout flow)
    ('placed', 'Placed'),
    ('confirmed', 'Confirmed'),
    ('preparing', 'Preparing'),
    ('ready', 'Ready'),
    ('picked_up', 'Picked Up'),
    # Terminal
    ('completed', 'Completed'),
    ('cancelled', 'Cancelled'),
]

ORDER_TYPES = [
    ('pickup', 'Pickup'),
    ('delivery', 'Delivery'),
    ('in_store', 'In-Store'),
]

PAYMENT_METHODS = [
    ('online', 'Online'),
    ('cash', 'Cash'),
    ('debit', 'Debit'),
    ('card', 'Card'),
]


class MintPosOrder(models.Model):
    _name = 'mint.pos.order'
    _description = 'POS Order'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'placed_at desc, id desc'

    name = fields.Char(
        string='Order Number',
        readonly=True,
        copy=False,
        default='New',
    )
    partner_id = fields.Many2one(
        'res.partner',
        string='Customer',
        index=True,
        tracking=True,
    )
    company_id = fields.Many2one(
        'res.company',
        string='Store',
        required=True,
        index=True,
        tracking=True,
        default=lambda self: self.env.company,
    )
    dutchie_checkout_id = fields.Char(
        string='Dutchie Checkout ID',
        index=True,
        copy=False,
    )
    dutchie_receipt_no = fields.Char(
        string='Dutchie Receipt #',
        index=True,
        copy=False,
    )
    state = fields.Selection(
        selection=ORDER_STATES,
        string='Status',
        default='online_orders',
        required=True,
        tracking=True,
        index=True,
        group_expand='_read_group_state',
    )
    lane_id = fields.Many2one(
        'mint.pos.lane',
        string='Lane',
        index=True,
        tracking=True,
        domain="[('company_id', '=', company_id)]",
        help='Per-store Dutchie swimlane. NULL for terminal-state orders. '
             'Source-of-truth for live orders; state mirrors lane.category. '
             'See mint.pos.lane.',
    )
    lane_id = fields.Many2one(
        'mint.pos.lane',
        string='Lane',
        index=True,
        tracking=True,
        domain="[('company_id', '=', company_id)]",
        help='Per-store Dutchie swimlane. NULL for terminal-state orders. '
             'Source-of-truth for live orders; state mirrors lane.category. '
             'See mint.pos.lane.',
    )
    order_type = fields.Selection(
        selection=ORDER_TYPES,
        string='Order Type',
        default='pickup',
        tracking=True,
    )
    payment_method = fields.Selection(
        selection=PAYMENT_METHODS,
        string='Payment Method',
        tracking=True,
    )
    subtotal = fields.Float(string='Subtotal', digits=(12, 2))
    discount_total = fields.Float(string='Discount Total', digits=(12, 2))
    tax_total = fields.Float(string='Tax Total', digits=(12, 2))
    total = fields.Float(string='Total', digits=(12, 2))

    line_ids = fields.One2many(
        'mint.pos.order.line', 'order_id',
        string='Order Lines',
    )
    line_count = fields.Integer(
        string='Item Count',
        compute='_compute_line_count',
        store=True,
    )
    # Live item count straight from Dutchie's checked-in feed (guest.TotalItems).
    # Distinct from line_count, which counts actual mint.pos.order.line records —
    # those stay 0 for live walk-ins (Dutchie exposes no in-progress cart) and
    # only populate at completion. Lets the kanban card show "N items" live.
    dutchie_item_count = fields.Integer(string='Items (live)', default=0)

    placed_at = fields.Datetime(
        string='Placed At',
        default=fields.Datetime.now,
        index=True,
    )
    confirmed_at = fields.Datetime(string='Confirmed At')
    ready_at = fields.Datetime(string='Ready At')
    completed_at = fields.Datetime(string='Completed At')

    budtender_id = fields.Many2one(
        'res.users',
        string='Budtender',
        tracking=True,
    )
    notes = fields.Text(string='Notes')

    loyalty_points_earned = fields.Integer(string='Points Earned')
    loyalty_points_redeemed = fields.Integer(string='Points Redeemed')

    is_prepaid = fields.Boolean(
        string='Prepaid',
        default=False,
        tracking=True,
    )
    dutchie_order_number = fields.Char(
        string='Dutchie Order #',
        index=True,
        copy=False,
    )
    payment_confirmed_at = fields.Datetime(string='Payment Confirmed At')
    dutchie_subtotal = fields.Float(string='Dutchie Subtotal', digits=(12, 2))
    dutchie_tax = fields.Float(string='Dutchie Tax', digits=(12, 2))
    dutchie_total = fields.Float(string='Dutchie Total', digits=(12, 2))

    # Web order item sync tracking (set by BullMQ webOrder processor)
    items_total = fields.Integer(string='Items Submitted', default=0)
    items_failed = fields.Integer(string='Items Failed', default=0)

    # Source tracking for deduplication (odoo_pos orders skip order-sync)
    source = fields.Selection(
        [
            ('web', 'Web Checkout'),
            ('dutchie_sync', 'Dutchie Sync'),
            ('dutchie_walkin', 'Dutchie Walk-in'),
            ('odoo_pos', 'Odoo POS'),
            ('walk_in', 'Walk-In'),
        ],
        string='Source',
        default='web',
        index=True,
    )

    # Dutchie shipment ID for lane-change relay
    dutchie_shipment_id = fields.Char(
        string='Dutchie Shipment ID',
        index=True,
        copy=False,
    )

    # ── Rich Dutchie transaction detail (populated by Dutchie sync) ───
    # Per-tender payment breakdown — a single transaction can mix
    # tenders; payment_method holds the primary, these hold the split.
    cash_paid = fields.Float(string='Cash Paid', digits=(12, 2))
    debit_paid = fields.Float(string='Debit Paid', digits=(12, 2))
    credit_paid = fields.Float(string='Credit Paid', digits=(12, 2))
    electronic_paid = fields.Float(string='Electronic Paid', digits=(12, 2))
    integrated_paid = fields.Float(string='Integrated Paid', digits=(12, 2))
    manual_paid = fields.Float(string='Manual Paid', digits=(12, 2))
    gift_paid = fields.Float(string='Gift Paid', digits=(12, 2))
    mmap_paid = fields.Float(string='MMAP Paid', digits=(12, 2))
    change_due = fields.Float(string='Change Due', digits=(12, 2))
    tip_amount = fields.Float(string='Tip', digits=(12, 2))
    # Dutchie returns the electronic-payment label (e.g. "Dutchie Pay", "Visa")
    payment_method_label = fields.Char(string='Payment Method Label')
    # Dutchie `loyaltyEarned` is a monetary value (float), distinct from
    # the existing Integer `loyalty_points_earned`. Kept separate so both
    # interpretations survive for accounting.
    loyalty_earned = fields.Float(string='Loyalty Earned ($)', digits=(12, 2))
    loyalty_spent = fields.Float(string='Loyalty Spent ($)', digits=(12, 2))

    # Order classification / origin
    order_source = fields.Char(
        string='Dutchie Order Source',
        index=True,
        help='Dutchie-side origin: "Walk In", "Dutchie", "Leafly", "Weedmaps", etc.',
    )
    was_preordered = fields.Boolean(string='Preordered')
    is_medical = fields.Boolean(string='Medical Transaction')

    # Customer enrichment (from Dutchie Guest record)
    dutchie_customer_id = fields.Integer(
        string='Dutchie Customer ID',
        index=True,
        help='Dutchie Guest_id — stable key to re-resolve if partner name changes.',
    )
    customer_type_id = fields.Integer(string='Customer Type ID')
    customer_dob = fields.Date(string='Customer DOB')
    customer_patient_type = fields.Char(string='Patient Type')
    customer_mj_state_id = fields.Char(string='MJ State ID')

    # Terminal / employee
    terminal_name = fields.Char(string='Register')
    dutchie_employee_id = fields.Integer(string='Dutchie Employee ID')
    checkin_at = fields.Datetime(string='Checked In At')

    # Computed: time since placed (for kanban color)
    wait_minutes = fields.Integer(
        string='Wait (min)',
        compute='_compute_wait_minutes',
    )

    _dutchie_checkout_uniq = models.Constraint(
        'UNIQUE(company_id, dutchie_checkout_id)',
        'Dutchie checkout ID must be unique per store.',
    )

    @api.model
    def _read_group_state(self, states=None, domain=None, order=None):
        """group_expand for the kanban: always render the live swimlanes.

        The Orders kanban groups by ``state``. Odoo only emits a column for a
        Selection group that already contains a record, so a store with no
        orders in (say) the Pick-Up lane would see that swimlane vanish — and a
        store with no active orders would see an empty board with no lanes at
        all. Expanding to a fixed set keeps every live lane visible per store.

        Scope = the live Dutchie lanes only. ``CATEGORY_TO_STATE`` (pos_lane.py)
        is the source of truth for the 1:1 lane<->state mapping, so its values,
        in flow order, ARE the real lane set. Legacy frontend states
        (placed/confirmed/preparing/ready) fold onto these via STATE_TO_CATEGORY
        and terminal states (completed/cancelled/picked_up/delivery_completed)
        are never lanes — both are intentionally excluded so they don't show as
        empty columns.
        """
        return list(CATEGORY_TO_STATE.values())

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', 'New') == 'New':
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'mint.pos.order'
                ) or 'New'
        records = super().create(vals_list)
        # Resolve lane_id for newly-created live orders so they don't sit in a
        # NULL lane until their first state write. Mirrors the write()
        # synchronizer's case-3 logic.
        records._sync_lane_from_state()
        # Live-push new live orders to the per-store kanban so a fresh walk-in
        # pops into its lane the moment it syncs — write() only pushes lane
        # *moves*, so without this a new transaction waits for a manual refresh.
        # Gate on non-terminal state + a company so historical/backfill imports
        # of completed orders don't spam the board or chime.
        for order in records:
            if order.state in self._TERMINAL_STATES or not order.company_id:
                continue
            self.env['bus.bus']._sendone(
                f'mint_pos_{order.company_id.id}',
                'order_update',
                {
                    'id': order.id,
                    'name': order.name,
                    'state': order.state,
                    'event': 'create',
                },
            )
        return records

    # Terminal states never have a Dutchie lane (Dutchie drops the order from
    # getCheckedInGuests once it hits these). `state` stays source-of-truth here.
    _TERMINAL_STATES = ('completed', 'cancelled', 'picked_up', 'delivery_completed')

    def _sync_lane_from_state(self):
        """Place each live order in its company's lane for the order's state.

        Maps state → lane category (legacy states fold onto the nearest live
        category), finds or creates that company's lane, and sets lane_id.
        Skips terminal-state orders (no lane) and orders that already match.
        Writes lane_id with from_dutchie_sync=True so no lane-change webhook
        fires from this internal bookkeeping.
        """
        Lane = self.env['mint.pos.lane'].sudo()
        for order in self:
            if order.state in self._TERMINAL_STATES or not order.company_id:
                continue
            target_category = STATE_TO_CATEGORY.get(order.state)
            if not target_category:
                continue
            if order.lane_id and order.lane_id.category == target_category:
                continue
            target = Lane.search(
                [
                    ('company_id', '=', order.company_id.id),
                    ('category', '=', target_category),
                    ('active', '=', True),
                ],
                order='sequence, id',
                limit=1,
            )
            if not target:
                target = Lane.find_or_create_by_dutchie_name(
                    order.company_id.id,
                    target_category.replace('_', ' ').title(),
                )
                if not target.category:
                    target.category = target_category
            # Explicit super() write: lane_id only, no state change, no
            # recursion into the synchronizer, no lane-change webhook.
            super(MintPosOrder, order).write({'lane_id': target.id})

    def write(self, vals):
        # ---- lane_id <-> state synchronizer (Phase 1 of swimlane-mirror) ----
        # Keep state and lane_id consistent without breaking any existing
        # state= writer. Three cases:
        #
        # 1. vals sets lane_id only → derive state from the lane's category.
        # 2. vals sets state to a terminal value → clear lane_id (Dutchie drops it).
        # 3. vals sets state to a non-terminal value → backfill lane_id post-write
        #    on records that don't already have one (handled after super().write()
        #    since we need per-record company_id).
        if vals.get('lane_id') and 'state' not in vals:
            lane = self.env['mint.pos.lane'].browse(vals['lane_id'])
            if lane.exists() and lane.category:
                mapped_state = CATEGORY_TO_STATE.get(lane.category)
                # employee/unassigned/other have no order state — leave it.
                if mapped_state:
                    vals['state'] = mapped_state
        if vals.get('state') in self._TERMINAL_STATES:
            vals['lane_id'] = False

        res = super().write(vals)
        new_state = vals.get('state')
        if not new_state:
            return res

        # Case 3: non-terminal state write, no lane_id explicitly set —
        # reconcile lane_id with the new state. Shares the create() logic.
        if (
            new_state not in self._TERMINAL_STATES
            and 'lane_id' not in vals
        ):
            self._sync_lane_from_state()

        now = fields.Datetime.now()

        # Set timestamp fields based on state transitions — batch the
        # records that still need the timestamp into a single write so
        # bulk state changes don't incur N extra UPDATEs.
        ts_map = {
            'confirmed': 'confirmed_at',
            'ready': 'ready_at',
            'pickup': 'ready_at',
            'deli_counter': 'ready_at',
            'completed': 'completed_at',
            'picked_up': 'completed_at',
            'delivery_completed': 'completed_at',
        }
        ts_field = ts_map.get(new_state)
        if ts_field:
            needs_stamp = self.filtered(lambda o: not o[ts_field])
            if needs_stamp:
                super(MintPosOrder, needs_stamp).write({ts_field: now})

        # Push notifications for customer-facing state changes.
        # Suppressed for system/sync dispositions (mint_pos_silent) — a guest
        # whose order we're auto-cancelling/-completing after they left should
        # NOT get a "your order was cancelled" push.
        if (new_state in self._get_notification_messages()
                and not self.env.context.get('mint_pos_silent')):
            for order in self:
                if order.partner_id:
                    self._send_order_notification(order, new_state)

        # Real-time bus.bus notification for Odoo UI
        for order in self:
            self.env['bus.bus']._sendone(
                f'mint_pos_{order.company_id.id}',
                'order_update',
                {
                    'id': order.id,
                    'name': order.name,
                    'state': new_state,
                    'event': 'update',
                },
            )

        # Relay lane change to Dutchie when dragged in Odoo kanban
        # Skip if this write came from Dutchie sync (avoid loop)
        if not self.env.context.get('from_dutchie_sync'):
            cancel_reason = self.env.context.get('cancel_reason') or ''
            for order in self:
                if order.dutchie_shipment_id:
                    self._fire_lane_change_webhook(order, new_state, cancel_reason)

        return res

    def _fire_lane_change_webhook(self, order, new_state, reason=''):
        """Fire webhook to inventory service to update Dutchie swimlane."""
        # No default URL — staging/dev Odoo without the config param set
        # would otherwise silently relay lane changes to prod mintinvsvc.
        # Set `mint_pos_dutchie.lane_change_webhook_url` per environment.
        webhook_url = self.env['ir.config_parameter'].sudo().get_param(
            'mint_pos_dutchie.lane_change_webhook_url', '',
        )
        if not webhook_url:
            return
        # The webhook is guarded by mintinvsvc's requireApiKey middleware,
        # which validates against its own API_KEY env var — a different secret
        # from the Odoo-side checkout_api_key (mintinvsvc uses one to
        # authenticate INTO Odoo, mintinvsvc accepts a different one IN). Use
        # a dedicated config param for this call; fall back to the legacy key
        # so existing environments don't break before the param is set.
        ICP = self.env['ir.config_parameter'].sudo()
        api_key = (
            ICP.get_param('mint_pos_dutchie.lane_change_webhook_api_key', '')
            or ICP.get_param('mint_customer_api.checkout_api_key', '')
        )

        payload = {
            'order_id': order.id,
            'order_ref': order.name,
            'new_state': new_state,
            'dutchie_shipment_id': order.dutchie_shipment_id,
            'company_id': order.company_id.id,
            'store_slug': order.company_id.x_slug if hasattr(order.company_id, 'x_slug') else '',
            'reason': reason or '',
        }
        # Capture the order's display name as a plain string BEFORE spawning
        # the daemon thread. The ORM recordset may be invalidated once the
        # request's cursor is closed, so accessing `order.name` inside
        # _post() would risk a stale-cursor error in the log call.
        order_name = str(order.name or '')

        def _post():
            import urllib.request
            try:
                data = json.dumps(payload).encode('utf-8')
                req = urllib.request.Request(
                    webhook_url,
                    data=data,
                    headers={
                        'Content-Type': 'application/json',
                        'X-Api-Key': api_key,
                    },
                    method='POST',
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    _logger.info(
                        'Lane change webhook fired for %s → %s (status %s)',
                        order_name, new_state, resp.status,
                    )
            except Exception:
                _logger.exception(
                    'Failed to fire lane change webhook for %s', order_name,
                )

        thread = threading.Thread(target=_post, daemon=True)
        thread.start()

    @api.model
    def _get_notification_messages(self):
        """Map of states → push notification content.

        Only states listed here trigger a customer push notification.
        Timing principle: notify when the customer should DO something
        or when something they care about changes.
        """
        return {
            # ── Order lifecycle (pickup) ────────────────────────
            'online_orders': {
                'title': 'Order Received',
                'body': '{store} received your order {ref}',
            },
            'confirmed': {
                'title': 'Order Confirmed',
                'body': '{store} confirmed your order {ref}',
            },
            'lobby': {
                'title': 'Checked In',
                'body': 'You are in the queue at {store}. We will call you up shortly.',
            },
            'sales_floor': {
                'title': 'Order Being Prepared',
                'body': 'A budtender is preparing your order {ref}',
            },
            'pickup': {
                'title': 'Ready for Pickup!',
                'body': 'Order {ref} is ready at the counter. Head to {store}!',
            },
            'deli_counter': {
                'title': 'Ready at Counter',
                'body': 'Order {ref} is waiting for you at {store}',
            },
            'ready': {
                'title': 'Ready for Pickup!',
                'body': 'Order {ref} is ready at {store}',
            },
            # ── Delivery ────────────────────────────────────────
            'delivery': {
                'title': 'Out for Delivery',
                'body': 'Order {ref} from {store} is on its way',
            },
            'delivery_progress': {
                'title': 'Driver En Route',
                'body': 'Your delivery from {store} is almost there',
            },
            'delivery_completed': {
                'title': 'Delivered',
                'body': 'Order {ref} from {store} has been delivered',
            },
            # ── Terminal ────────────────────────────────────────
            'cancelled': {
                'title': 'Order Cancelled',
                'body': 'Order {ref} at {store} has been cancelled',
            },
        }

    def _send_order_notification(self, order, state):
        """Send push notification to customer on state change."""
        messages = self._get_notification_messages()
        msg = messages.get(state)
        if not msg:
            return

        try:
            store_name = order.company_id.name or ''
            ref = order.name or ''
            title = msg['title']
            body = msg['body'].format(store=store_name, ref=ref)
            order_url = f'/orders?ref={ref}'

            # sudo(): mint.push.subscription is stored on the root company
            # and accessed across stores; write() runs as the triggering
            # user which may not have direct model access.
            self.env['mint.push.subscription'].sudo().send_to_partner(
                partner_id=order.partner_id.id,
                title=title,
                body=body,
                url=order_url,
                actions=[
                    {'action': 'track', 'title': 'Track Order', 'url': order_url},
                ],
            )
            _logger.info(
                'Push [%s] sent for order %s to partner %s',
                state, order.name, order.partner_id.id,
            )
        except Exception:
            _logger.exception(
                'Failed to send push notification for order %s', order.name,
            )

    @api.depends('line_ids')
    def _compute_line_count(self):
        for order in self:
            order.line_count = len(order.line_ids)

    @api.depends('placed_at', 'state')
    def _compute_wait_minutes(self):
        now = fields.Datetime.now()
        for order in self:
            if order.placed_at and order.state in ('placed', 'confirmed', 'preparing'):
                delta = now - order.placed_at
                order.wait_minutes = int(delta.total_seconds() / 60)
            else:
                order.wait_minutes = 0

    # ── Action buttons for form view ──────────────────────────────────

    def action_confirm(self):
        self.filtered(lambda o: o.state == 'placed').write({'state': 'confirmed'})

    def action_start_preparing(self):
        self.filtered(lambda o: o.state == 'confirmed').write({'state': 'preparing'})

    def action_mark_ready(self):
        self.filtered(lambda o: o.state in ('confirmed', 'preparing')).write({'state': 'ready'})

    def action_mark_picked_up(self):
        self.filtered(lambda o: o.state == 'ready').write({'state': 'picked_up'})

    def action_complete(self):
        self.filtered(lambda o: o.state in ('ready', 'picked_up')).write({'state': 'completed'})

    def action_cancel(self):
        self.filtered(lambda o: o.state not in ('completed', 'cancelled')).write({'state': 'cancelled'})

    # ── Cron: cancel abandoned active-lane orders ─────────────────────
    #
    # Customers sometimes hit lobby / sales_floor / pickup in Dutchie's
    # swimlane and never finalize (no-show, walk-out, register error,
    # budtender forgets to manually transition the lane). Those rows
    # then sit on the kanban as $0/0-item ghost cards forever, hiding
    # real-time activity behind clutter.
    #
    # Scheduled to run daily at 10:00 UTC (≈ 3am MST / 4am MDT) when
    # all stores are closed — see data/cron.xml. Targets every order
    # with create_date older than 24h whose state is still in the
    # active lanes, and bulk-writes state='cancelled'. Completed and
    # already-cancelled rows are not touched.
    #
    # Reversible: a misclassified row can be flipped back via the form
    # view's status bar.
    ACTIVE_LANE_STATES = (
        'lobby', 'online_orders', 'sales_floor', 'processing',
        'pickup', 'deli_counter', 'credit_checkout',
        'placed', 'confirmed', 'preparing', 'ready',
    )

    # Context that makes a terminal write SILENT: no customer push, no Dutchie
    # lane-change webhook (the guest already left), and no chatter tracking
    # (keeps bulk cleanups from writing thousands of mail rows).
    _SILENT_DISPOSE_CTX = {
        'mint_pos_silent': True,
        'from_dutchie_sync': True,
        'tracking_disable': True,
    }

    def _dispose_departed(self):
        """Cancel ONLY empty no-show stubs whose guest left Dutchie's checked-in set.

        A departed order that carries a cart (``total`` > 0 or
        ``dutchie_item_count`` > 0) is a probable completed sale — leave it for
        the transaction sync (orderSync, report 1082) to finalize, and the >24h
        cron to clean if it genuinely never sold. We NEVER cancel a carted order
        from the reconcile path: the 2026-06-08 incident cancelled carted
        departures and mis-recorded 738 real sales (see task #95531). Completion
        is owned solely by the txn-sync, so this method does not mark anything
        'completed' either. Writes silently (no push / no webhook / no chatter).
        Returns the count cancelled.
        """
        live = self.filtered(lambda o: o.state not in self._TERMINAL_STATES)
        empty = live.filtered(lambda o: not o.total and not o.dutchie_item_count)
        if not empty:
            return 0
        empty.with_context(**self._SILENT_DISPOSE_CTX).write({'state': 'cancelled'})
        _logger.info(
            'mint.pos.order: cancelled %d departed empty stubs '
            '(left %d carted departures for the txn-sync)',
            len(empty), len(live) - len(empty),
        )
        return len(empty)

    @api.model
    def _cron_cancel_stale_orders(self, hours_old=24):
        cutoff = fields.Datetime.now() - timedelta(hours=hours_old)
        stale = self.search([
            ('create_date', '<', cutoff),
            ('state', 'in', list(self.ACTIVE_LANE_STATES)),
        ])
        if not stale:
            _logger.info(
                'mint.pos.order: no stale active-lane orders older than %sh',
                hours_old,
            )
            return 0
        count = len(stale)
        # SILENT: an order stale for >24h is abandoned — cancel it without
        # spamming the customer a push or relaying a lane change to Dutchie.
        # (Firing those on a bulk run is what got this cron disabled before.)
        stale.with_context(**self._SILENT_DISPOSE_CTX).write({'state': 'cancelled'})
        _logger.info(
            'mint.pos.order: cancelled %d stale active-lane orders older than %sh',
            count, hours_old,
        )
        return count


class MintPosOrderLine(models.Model):
    _name = 'mint.pos.order.line'
    _description = 'POS Order Line'
    _order = 'sequence, id'

    order_id = fields.Many2one(
        'mint.pos.order',
        string='Order',
        required=True,
        ondelete='cascade',
        index=True,
    )
    sequence = fields.Integer(string='Sequence', default=10)

    product_name = fields.Char(string='Product', required=True)
    dutchie_product_id = fields.Char(string='Dutchie Product ID')
    sku = fields.Char(string='SKU')

    quantity = fields.Float(string='Qty', default=1.0)
    unit_price = fields.Float(string='Unit Price', digits=(12, 2))
    discount = fields.Float(string='Discount', digits=(12, 2))
    tax = fields.Float(string='Tax', digits=(12, 2))
    line_total = fields.Float(
        string='Line Total',
        compute='_compute_line_total',
        store=True,
        digits=(12, 2),
    )

    category = fields.Char(string='Category')
    brand = fields.Char(string='Brand')
    strain_type = fields.Char(string='Strain Type')
    weight = fields.Char(string='Weight')

    # ── Rich Dutchie line detail (populated by Dutchie sync) ─────────
    # `sku` holds the batch name (existing convention); dutchie_package_id
    # is the distinct Dutchie package identifier for this line.
    dutchie_package_id = fields.Char(string='Dutchie Package ID', index=True)
    dutchie_inventory_id = fields.Integer(string='Dutchie Inventory ID')
    # Stable Dutchie-assigned unique key for a line (safe to dedupe on).
    dutchie_transaction_item_id = fields.Integer(
        string='Dutchie Txn Item ID',
        index=True,
    )
    unit_cost = fields.Float(string='Unit Cost', digits=(12, 2))
    vendor = fields.Char(string='Vendor')
    # Numeric weight + unit pair (the existing `weight` Char remains for
    # human display like "3.5g"; these are for analytics/margin).
    unit_weight_value = fields.Float(string='Unit Weight Value', digits=(12, 4))
    unit_weight_unit = fields.Char(string='Unit Weight Unit')
    is_returned = fields.Boolean(string='Returned')
    return_date = fields.Datetime(string='Return Date')
    return_reason = fields.Char(string='Return Reason')
    # Preserve the per-line discount/tax breakdown Dutchie returns. The
    # scalar `discount` and `tax` above are rollups; these keep the full
    # arrays (discount name + id + amount per applied discount; rate +
    # amount per tax) so margin/compliance reports can rebuild them.
    dutchie_discounts_json = fields.Text(string='Dutchie Discounts JSON')
    dutchie_taxes_json = fields.Text(string='Dutchie Taxes JSON')

    # Related fields for filtering
    company_id = fields.Many2one(
        related='order_id.company_id',
        string='Store',
        store=True,
        index=True,
    )

    @api.depends('quantity', 'unit_price', 'discount')
    def _compute_line_total(self):
        for line in self:
            line.line_total = (line.quantity * line.unit_price) - line.discount
