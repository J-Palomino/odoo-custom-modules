# -*- coding: utf-8 -*-
{
    'name': 'Mint Account',
    'version': '19.0.1.0.0',
    'category': 'Website',
    'summary': 'Customer account, cart persistence, order lookup for MintDeals frontend',
    'description': """
        Mint Account — headless customer account backend for letsgomint.us.

        Provides JWT-authenticated REST endpoints for the Astro frontend:
          - /api/v1/auth/{login,register,forgot-password,reset-password,me}
          - /api/v1/cart           — per-partner, per-store persistent cart
          - /api/v1/cart/merge     — union local cart with server cart on login
          - /api/v1/orders[/:ref]  — list/detail of partner's pos.order records

        Carts are keyed by (partner_id, company_id) for Florida-compliant
        per-region isolation. Orders are read-only; fulfillment stays in
        Dutchie via the existing mint_pos_bridge.
    """,
    'author': 'Mint Dispensaries',
    'website': 'https://letsgomint.us',
    'license': 'LGPL-3',
    'depends': [
        'base',
        'mail',
        'auth_signup',
        'mint_pos_bridge',
    ],
    'data': [
        'security/ir.model.access.csv',
        'data/ir_config_params.xml',
    ],
    'installable': True,
    'auto_install': False,
    'application': False,
}
