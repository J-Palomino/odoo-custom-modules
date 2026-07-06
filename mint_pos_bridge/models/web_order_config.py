# -*- coding: utf-8 -*-
"""
Web Order Configuration

Maps Odoo order states to Dutchie POS swimlane lanes and push notification templates.
Configurable per store via Odoo admin UI.
"""
import json
import logging
from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class WebOrderConfig(models.Model):
    _name = 'mint.web.order.config'
    _description = 'Web Order Configuration'
    _order = 'company_id, sequence'

    company_id = fields.Many2one(
        'res.company',
        string='Store',
        required=True,
        index=True,
        default=lambda self: self.env.company,
    )
    active = fields.Boolean(default=True)

    # Dutchie POS connection
    dutchie_loc_id = fields.Integer(string='Dutchie LocId', required=True,
                                     help='Dutchie POS location ID (e.g. 1568 for Tempe)')
    dutchie_register_id = fields.Integer(string='Register ID', default=0,
                                          help='POS register to assign orders to (0 = none; register/room IDs are per-location in Dutchie)')
    dutchie_room_id = fields.Integer(string='Room ID', default=0,
                                      help='POS room for check-in — set to THIS location\'s room; IDs are per-location in Dutchie')

    # State-to-lane mapping (stored as JSON)
    state_lane_map = fields.Text(
        string='State-Lane Mapping (JSON)',
        default='{"lobby":"Lobby","online_orders":"Online Orders","sales_floor":"Sales Floor/ Needs Code","processing":"Processing","pickup":"Pick-Up","deli_counter":"Deli Counter","credit_checkout":"CREDIT CHECKOUT","delivery":"Delivery","ready_delivery":"Ready For Delivery","delivery_progress":"Delivery In Progess","delivery_completed":"Delivery Completed","completed":"","cancelled":""}',
        help='JSON mapping of Odoo order states to Dutchie POS swimlane lane names. Must match exact lane names in Dutchie backoffice.',
    )

    # Push notification templates (stored as JSON)
    push_templates = fields.Text(
        string='Push Notification Templates (JSON)',
        default='{"online_orders":{"title":"Order Received","body":"Your order at {store} has been placed. We will start preparing it shortly."},"processing":{"title":"Being Prepared","body":"Your order at {store} is being prepared now."},"pickup":{"title":"Ready for Pickup!","body":"Your order at {store} is ready! Head to the pickup counter."},"cancelled":{"title":"Order Cancelled","body":"Your order at {store} has been cancelled. Contact us if you have questions."}}',
        help='JSON templates for push notifications per state. Use {store}, {order_ref}, {customer} placeholders.',
    )

    # Order source settings
    order_source = fields.Char(string='Order Source Label', default='MintDeals',
                                help='How orders appear in Dutchie POS (e.g. Dutchie, MintDeals)')
    default_payment_method = fields.Selection([
        ('cash', 'Cash'),
        ('debit', 'Debit'),
        ('card', 'Card'),
    ], string='Default Payment Method', default='cash')

    # Feature toggles
    auto_sync_to_pos = fields.Boolean(string='Auto-sync to POS', default=True,
                                       help='Automatically push web orders to Dutchie POS swimlane')
    auto_push_notifications = fields.Boolean(string='Auto Push Notifications', default=True,
                                               help='Send push notifications on state changes')
    require_photo_id = fields.Boolean(string='Require Photo ID', default=False)

    sequence = fields.Integer(default=10)

    _company_uniq = models.Constraint(
        'UNIQUE(company_id)',
        'Only one web order config per store.',
    )

    def get_lane_for_state(self, state):
        """Get the Dutchie POS swimlane lane name for an order state."""
        try:
            mapping = json.loads(self.state_lane_map or '{}')
            return mapping.get(state, '')
        except (json.JSONDecodeError, TypeError):
            return ''

    def get_push_template(self, state):
        """Get push notification template for an order state."""
        try:
            templates = json.loads(self.push_templates or '{}')
            return templates.get(state, {})
        except (json.JSONDecodeError, TypeError):
            return {}

    def format_push_message(self, state, store_name='', order_ref='', customer_name=''):
        """Format a push notification for the given state with placeholders filled."""
        template = self.get_push_template(state)
        if not template:
            return None, None

        title = (template.get('title', '') or '').replace('{store}', store_name).replace('{order_ref}', order_ref).replace('{customer}', customer_name)
        body = (template.get('body', '') or '').replace('{store}', store_name).replace('{order_ref}', order_ref).replace('{customer}', customer_name)
        return title, body


class WebOrderConfigAPI(models.AbstractModel):
    """API helper to serve config to BullMQ workers."""
    _name = 'mint.web.order.config.api'
    _description = 'Web Order Config API Helper'

    @api.model
    def get_config_for_store(self, store_slug):
        """Get web order configuration for a store by slug."""
        company = self.env['res.company'].sudo().search(
            [('x_slug', '=', store_slug)], limit=1)
        if not company:
            return {}

        config = self.env['mint.web.order.config'].sudo().search(
            [('company_id', '=', company.id), ('active', '=', True)], limit=1)
        if not config:
            return {}

        try:
            state_lane_map = json.loads(config.state_lane_map or '{}')
        except json.JSONDecodeError:
            state_lane_map = {}

        try:
            push_templates = json.loads(config.push_templates or '{}')
        except json.JSONDecodeError:
            push_templates = {}

        return {
            'store_slug': store_slug,
            'company_id': company.id,
            'store_name': company.name,
            'dutchie_loc_id': config.dutchie_loc_id,
            'dutchie_register_id': config.dutchie_register_id,
            'dutchie_room_id': config.dutchie_room_id,
            'state_lane_map': state_lane_map,
            'push_templates': push_templates,
            'order_source': config.order_source,
            'default_payment_method': config.default_payment_method,
            'auto_sync_to_pos': config.auto_sync_to_pos,
            'auto_push_notifications': config.auto_push_notifications,
            'require_photo_id': config.require_photo_id,
        }

    @api.model
    def get_all_configs(self):
        """Get all active web order configurations.

        Returns a dict keyed by store slug. Each entry mirrors the shape of
        get_config_for_store() so the BullMQ lane watcher can build its
        per-store state_lane_map without a second round-trip.
        """
        configs = self.env['mint.web.order.config'].sudo().search([('active', '=', True)])
        result = {}
        for config in configs:
            slug = config.company_id.x_slug
            if not slug:
                continue
            try:
                state_lane_map = json.loads(config.state_lane_map or '{}')
            except json.JSONDecodeError:
                state_lane_map = {}
            try:
                push_templates = json.loads(config.push_templates or '{}')
            except json.JSONDecodeError:
                push_templates = {}
            result[slug] = {
                'company_id': config.company_id.id,
                'store_name': config.company_id.name,
                'dutchie_loc_id': config.dutchie_loc_id,
                'dutchie_register_id': config.dutchie_register_id,
                'dutchie_room_id': config.dutchie_room_id,
                'state_lane_map': state_lane_map,
                'push_templates': push_templates,
                'online_orders_lane': config.get_lane_for_state('placed'),
                'order_source': config.order_source,
                'default_payment_method': config.default_payment_method,
                'auto_sync_to_pos': config.auto_sync_to_pos,
                'auto_push_notifications': config.auto_push_notifications,
                'require_photo_id': config.require_photo_id,
            }
        return result
