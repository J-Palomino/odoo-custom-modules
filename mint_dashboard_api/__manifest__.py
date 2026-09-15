# -*- coding: utf-8 -*-
{
    'name': 'Mint Marketing Dashboard API',
    'version': '19.0.0.1.0',
    'category': 'Website',
    'summary': 'Employee-authenticated REST API for the marketing ops dashboard (Odoo #103759)',
    'description': """
        Mint Marketing Dashboard API
        ============================

        REST endpoints at /api/v1/dashboard/ serving the internal
        marketing/retail-ops dashboard during its migration off Databasepad
        (Odoo epic #103759).

        - Employee JWT auth (reuses mint_customer_api res.users JWT infra;
          rejects web:-prefixed customer accounts)
        - mint.dashboard.permission: per-module {view,edit,delete} flags +
          market access, enforced server-side on every route
        - Phase 0 surface: health, auth, markets, locations, profile
    """,
    'author': 'MintDeals',
    'website': 'https://mintdeals.com',
    'license': 'LGPL-3',
    'depends': [
        'base',
        'mint_api_v2',
        'mint_customer_api',
    ],
    'data': [
        'security/ir.model.access.csv',
    ],
    'installable': True,
    'application': False,
}
