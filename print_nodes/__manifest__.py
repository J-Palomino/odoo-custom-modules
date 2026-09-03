# -*- coding: utf-8 -*-
{
    'name': 'PrintNodes',
    'version': '19.0.1.0.0',
    'category': 'Point of Sale',
    'summary': 'Self-hosted label/receipt printing for POS — Zebra ZD410, '
               'store print nodes + job queue + label designer',
    'description': """
        PrintNodes — print labels and receipts from the POS to printers
        connected to a store's print node, PrintNode-style but self-hosted.

        Features:
          - Zebra ZD410 (ZPL) exit labels, receipts, and METRC product labels
          - Label Designer with live preview (Labelary) + print-to-node
          - Store print nodes + printers + a print-job queue
          - Local agent (localhost + poll modes); WebUSB / local-agent /
            PrintNode transports from the POS

        Independent of any Dutchie/inventory integration — depends only on
        Point of Sale + Stock, and exposes only token-authenticated print
        routes (no externally-driven endpoints).
    """,
    'author': 'Mint Dispensaries',
    'website': 'https://mintdispensaries.com',
    'license': 'LGPL-3',
    'depends': [
        'point_of_sale',
        'stock',
    ],
    'data': [
        'security/ir.model.access.csv',
        'security/record_rules.xml',
        'views/print_node_views.xml',
        'views/zebra_label_views.xml',
        'views/pos_config_views.xml',
    ],
    'assets': {
        'point_of_sale._assets_pos': [
            'print_nodes/static/src/app/**/*',
        ],
    },
    'installable': True,
    'auto_install': False,
    'application': False,
}
