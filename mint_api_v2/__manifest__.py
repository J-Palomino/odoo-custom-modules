# -*- coding: utf-8 -*-
{
    'name': 'MintDeals REST API v2',
    'version': '19.0.4.31.0',
    'category': 'Website',
    'summary': 'REST API endpoints for MintDeals frontend (native Odoo controllers)',
    'description': """
        MintDeals REST API v2
        =====================

        Exposes REST API endpoints at /api/v1/ for:
        - Stores (locations with hours, amenities, services)
        - Products (inventory with pricing, potency)
        - Discounts (deals with targeting rules)
        - Blog posts
        - Events

        Uses Odoo's native HTTP controllers for maximum compatibility.
    """,
    'author': 'MintDeals',
    'website': 'https://mintdeals.com',
    'license': 'LGPL-3',
    'depends': [
        'base',
        'mail',
        'product',
        'stock',
        'website',
        'loyalty',
    ],
    'data': [
        'security/ir.model.access.csv',
        'security/product_visibility_rules.xml',
        'views/res_company_views.xml',
        'views/mint_discount_views.xml',
        'views/mint_spin_views.xml',
        'data/spin_config.xml',
        'views/product_template_views.xml',
        'views/mint_strain_views.xml',
        'data/ir_cron.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
