# -*- coding: utf-8 -*-
{
    'name': 'MintDeals Customer API',
    'version': '19.0.2.21.0',
    'category': 'Website',
    'summary': 'Customer auth, checkout, and loyalty API for MintDeals frontend',
    'author': 'MintDeals',
    'website': 'https://mintdeals.com',
    'license': 'LGPL-3',
    'depends': [
        'base',
        'portal',
        'auth_signup',
        'sale_management',
        'loyalty',
        'crm',
        'mint_api_v2',
        # Owns x_partner_origin (and pulls in mint_dutchie_sync, which owns
        # x_dutchie_customer_id) — both referenced by the customer-hiding
        # record rule and the partner search filters.
        'mint_pos_bridge',
    ],
    'external_dependencies': {
        'python': ['PyJWT'],
    },
    'data': [
        'security/ir.model.access.csv',
        'security/web_customer_security.xml',
        'views/res_partner_views.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
