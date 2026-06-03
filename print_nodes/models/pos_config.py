# -*- coding: utf-8 -*-
"""Extend pos.config with PrintNodes (Zebra) per-register settings."""
from odoo import api, fields, models


class PosConfig(models.Model):
    _inherit = 'pos.config'

    mint_zebra_enabled = fields.Boolean(
        string='Enable Zebra ZD410 Printing',
        default=False,
        help='Print exit labels and/or receipts to a local Zebra ZD410 (ZPL) '
             'from this register, via WebUSB, a local print agent, or PrintNode.',
    )
    mint_zebra_transport = fields.Selection(
        [
            ('auto', 'Auto (WebUSB → local agent → PrintNode)'),
            ('webusb', 'WebUSB only (direct, Chromium)'),
            ('local_agent', 'Local agent only (localhost)'),
            ('printnode', 'PrintNode only (cloud)'),
        ],
        string='Zebra Transport',
        default='auto',
        help='How the browser reaches the printer. Auto tries a paired USB '
             'printer, then the local agent, then PrintNode.',
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
             'Parameter "print_nodes.printnode_api_key".',
    )

    @api.model
    def _load_pos_data_fields(self, config_id):
        """Expose PrintNodes settings to the POS frontend (not the secret key)."""
        fields_list = super()._load_pos_data_fields(config_id)
        return fields_list + [
            'mint_zebra_enabled',
            'mint_zebra_transport',
            'mint_zebra_autoprint',
            'mint_zebra_print_label',
            'mint_zebra_print_receipt',
        ]
