# -*- coding: utf-8 -*-
{
    'name': 'Mint POS ↔ Dutchie Bridge',
    'version': '19.0.1.0.0',
    'category': 'Point of Sale',
    'summary': 'Odoo POS checkout with Dutchie compliance relay + swimlane management',
    'description': """
        Bridges Odoo's native point_of_sale with Dutchie POS for cannabis
        dispensary operations.

        Features:
          - Ring up customers in Odoo POS with live Dutchie inventory
          - Auto-relay completed sales to Dutchie for METRC/BioTrack compliance
          - POS categories mapped from cannabis master_category
          - Inventory quantity sync (Dutchie → Odoo stock)
          - Bidirectional swimlane state sync (Odoo kanban ↔ Dutchie lanes)
          - Retry UI for failed Dutchie syncs
    """,
    'author': 'Mint Dispensaries',
    'website': 'https://mintdispensaries.com',
    'license': 'LGPL-3',
    'depends': [
        'point_of_sale',
        'stock',
        'mint_pos_bridge',
        'mint_api_v2',
    ],
    'data': [
        'security/ir.model.access.csv',
        'data/pos_category_data.xml',
        'views/pos_order_views.xml',
        'views/pos_config_views.xml',
    ],
    'installable': True,
    'auto_install': False,
    'application': False,
}
