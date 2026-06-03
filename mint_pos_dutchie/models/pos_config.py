# -*- coding: utf-8 -*-
"""
Extend pos.config with Dutchie POS connection settings.

Each POS configuration (one per store register) stores the Dutchie
location, register, and room IDs needed to relay sales.
"""
from odoo import fields, models


class PosConfig(models.Model):
    _inherit = 'pos.config'

    dutchie_enabled = fields.Boolean(
        string='Enable Dutchie Relay',
        default=False,
        help='When enabled, completed POS sales are relayed to Dutchie for METRC/BioTrack compliance.',
    )
    dutchie_loc_id = fields.Integer(
        string='Dutchie Location ID',
        help='Dutchie POS location ID (e.g., 1568 for Tempe).',
    )
    dutchie_register_id = fields.Integer(
        string='Dutchie Register ID',
        help='Dutchie register to assign orders to (e.g., 5115 for ADMIN 1).',
    )
    dutchie_room_id = fields.Integer(
        string='Dutchie Room ID',
        help='Dutchie room for check-in (e.g., 16071 for Sales Floor).',
    )
