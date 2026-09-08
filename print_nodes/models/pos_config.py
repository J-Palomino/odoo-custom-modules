# -*- coding: utf-8 -*-
"""Extend pos.config with PrintNodes (Zebra) per-register settings."""
from odoo import api, fields, models

from . import zebra_zpl


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
    mint_escpos_open_drawer = fields.Boolean(
        string='Open Cash Drawer on Receipt',
        default=True,
        help='For ESC/POS receipt printers (Star/Epson), append a drawer-kick '
             'pulse to each printed receipt so the connected cash drawer opens. '
             'No effect on Zebra/ZPL printers.',
    )
    mint_print_label_on_scan = fields.Boolean(
        string='Print Label on Barcode Scan',
        default=False,
        help='When a product barcode is scanned at this register, also print a '
             'product/shelf label for it — in addition to Odoo adding the item '
             'to the order. Uses the register\'s local print agent + label '
             'printer.',
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
            'mint_escpos_open_drawer',
            'mint_print_label_on_scan',
        ]

    def mint_scan_product_label_zpl(self, barcode):
        """Return ZPL for a scanned product's label (Print-Label-on-Scan).

        Called from the POS when a product barcode is scanned and
        ``mint_print_label_on_scan`` is on. Resolves the product by barcode,
        then by internal reference, and builds a simple product/shelf label
        from core product fields only. Compliance fields (THC/CBD/lot) are left
        blank — they need per-catalog product-field mapping and can be added
        once those fields are confirmed on the live DB. Returns '' when nothing
        matches, so the POS can stay quiet (Odoo's own not-found note applies).
        """
        self.ensure_one()
        code = (barcode or '').strip()
        if not code:
            return ''
        Product = self.env['product.product']
        product = (Product.search([('barcode', '=', code)], limit=1)
                   or Product.search([('default_code', '=', code)], limit=1))
        if not product:
            return ''
        company = self.company_id or self.env.company
        store_line = ' | '.join(p for p in (company.name, company.street) if p)
        data = {
            'store_line': store_line,
            'product_name': product.display_name,
            'barcode': code,
        }
        dpi = int(self.mint_zebra_dpi or '203')
        return zebra_zpl.build_product_label_zpl(data, dpi=dpi)
