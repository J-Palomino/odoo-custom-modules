# -*- coding: utf-8 -*-
{
    'name': 'MintDeals REST API v2',
    'version': '19.0.2.0.0',
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
        'product',
        'stock',
        'website',
    ],
    'data': [
        'security/ir.model.access.csv',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
