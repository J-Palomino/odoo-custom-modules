# -*- coding: utf-8 -*-
{
    'name': 'Avancir Inventory Integration',
    'version': '19.0.2.0.0',
    'category': 'Inventory',
    'summary': 'Sync inventory with Avancir API',
    'description': """
        Avancir Inventory Integration
        =============================

        This module provides integration between Odoo and Avancir inventory management system.

        Features:
        - Bulk sync products to Avancir
        - Bulk sync inventory quantities
        - Automatic scheduled sync
        - Manual sync from product form
        - Per-company/warehouse sync support
        - Inter-store inventory transfers
        - End-of-day POS reconciliation

        Configuration:
        - Set Avancir API credentials in Settings > Inventory > Avancir
        - Map Odoo fields to Avancir item types
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
# Build cache bust: 1769454700
