# -*- coding: utf-8 -*-
{
    'name': 'MintDeals Customer API',
    'version': '19.0.2.8.0',
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
    ],
    'external_dependencies': {
        'python': ['PyJWT'],
    },
    'data': [
        'security/ir.model.access.csv',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
