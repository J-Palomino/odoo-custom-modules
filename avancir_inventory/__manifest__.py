# -*- coding: utf-8 -*-
{
    'name': 'RFID Inventory Integration',
    'version': '19.0.5.0.0',
    'category': 'Inventory',
    'summary': 'RFID-based inventory sync and tracking',
    'description': """
        RFID Inventory Integration
        ==========================

        This module provides RFID-based inventory management and synchronization.

        Features:
        - Bulk sync products via RFID
        - Bulk sync inventory quantities
        - Automatic scheduled sync
        - Manual sync from product form
        - Per-company/warehouse sync support
        - Inter-store inventory transfers
        - End-of-day POS reconciliation
        - Fetch activity history from Avancir API

        Configuration:
        - Set RFID API credentials in Settings > Inventory > RFID
        - Map Odoo fields to RFID item types
        - Configure sync schedule
    """,
    'author': 'MintDeals',
    'website': 'https://mintdeals.com',
    'license': 'LGPL-3',
    'depends': [
        'base',
        'product',
        'stock',
        'sale_management',
    ],
    'data': [
        'security/ir.model.access.csv',
        'views/res_config_settings_views.xml',
        'views/product_template_views.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
