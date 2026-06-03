# -*- coding: utf-8 -*-
"""
Extend pos.config with Dutchie POS connection settings.

Each POS configuration (one per store register) stores the Dutchie
location, register, and room IDs needed to relay sales.
"""
from odoo import api, fields, models


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

    # ── Zebra ZD410 local receipt / exit-label printing ──────────────
    mint_zebra_enabled = fields.Boolean(
        string='Enable Zebra ZD410 Printing',
        default=False,
        help='Print exit labels and/or receipts to a local Zebra ZD410 (ZPL) '
             'from this register, via WebUSB (direct) or PrintNode (cloud).',
    )
    mint_zebra_transport = fields.Selection(
        [
            ('auto', 'Auto (WebUSB → PrintNode)'),
            ('webusb', 'WebUSB only (direct, Chromium)'),
            ('printnode', 'PrintNode only (cloud)'),
        ],
        string='Zebra Transport',
        default='auto',
        help='How the browser reaches the printer. Auto tries a paired USB '
             'printer first, then falls back to PrintNode.',
    )
    mint_zebra_autoprint = fields.Boolean(
        string='Auto-print on payment',
        default=True,
        help='Automatically print when the receipt screen opens after payment.',
    )
    mint_zebra_print_label = fields.Boolean(
        string='Print Exit Label',
        default=True,
    )
    mint_zebra_print_receipt = fields.Boolean(
        string='Print Receipt',
        default=True,
    )
    mint_zebra_dpi = fields.Selection(
        [('203', '203 dpi'), ('300', '300 dpi')],
        string='Zebra DPI',
        default='203',
        help='ZD410 print density. Standard ZD410 is 203 dpi.',
    )
    mint_printnode_printer_id = fields.Integer(
        string='PrintNode Printer ID',
        help='Numeric printer id from PrintNode (GET /printers) for the ZD410 '
             'attached to this register. Used when transport falls back to '
             'PrintNode. The PrintNode API key is stored globally in System '
             'Parameter "mint_pos_dutchie.printnode_api_key".',
    )

    @api.model
    def _load_pos_data_fields(self, config_id):
        """Expose Zebra settings to the POS frontend (not the secret key)."""
        fields_list = super()._load_pos_data_fields(config_id)
        return fields_list + [
            'mint_zebra_enabled',
            'mint_zebra_transport',
            'mint_zebra_autoprint',
            'mint_zebra_print_label',
            'mint_zebra_print_receipt',
        ]
