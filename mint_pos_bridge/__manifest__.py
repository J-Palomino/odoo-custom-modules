# -*- coding: utf-8 -*-
{
    'name': 'Mint POS Bridge',
    'version': '19.0.4.8.0',
    'category': 'Operations',
    'summary': 'Dutchie POS ↔ Odoo order bridge — unified dispensary operations',
    'description': """
        Mint POS Bridge — headless Dutchie integration for Mint Cannabis.

        Makes Odoo the single UI for all dispensary operations. Dutchie runs
        headless — no user ever sees Dutchie's interface.

        Features:
          - POS order tracking with state machine (placed → completed)
          - REST API for BullMQ order sync and frontend checkout
          - Push notifications when orders are ready
          - Kanban board for budtender order management
          - Multi-company isolation (per-store orders)
          - Employee credential management for Dutchie access
    """,
    'author': 'Mint Dispensaries',
    'website': 'https://mintdispensaries.com',
    'license': 'LGPL-3',
    'depends': [
        'base',
        'mail',
        'bus',
        'hr',
        'mint_push',
        'mint_dutchie_sync',
        'mint_command_center',
        'mint_api_v2',
    ],
    'data': [
        'security/security_groups.xml',
        'security/ir.model.access.csv',
        'security/record_rules.xml',
        'data/sequence.xml',
        'views/pos_order_views.xml',
        'views/pos_order_menus.xml',
        'views/web_order_config_views.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'mint_pos_bridge/static/src/css/pos_kanban.css',
            'mint_pos_bridge/static/src/js/pos_kanban_live.js',
        ],
    },
    'installable': True,
    'auto_install': False,
    'application': True,
}
