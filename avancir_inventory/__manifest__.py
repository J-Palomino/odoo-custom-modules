# -*- coding: utf-8 -*-
{
    'name': 'Avancir Inventory Integration',
    'version': '19.0.1.0.0',
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
        'data/ir_cron.xml',
        'views/res_config_settings_views.xml',
        'views/product_template_views.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
