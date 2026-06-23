# -*- coding: utf-8 -*-
{
    'name': 'MintDeals Customer API',
    'version': '19.0.2.17.0',
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
        'mint_api_v2',
    ],
    'external_dependencies': {
        'python': ['PyJWT'],
    },
    'data': [
        'security/ir.model.access.csv',
        'security/web_customer_security.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
