# -*- coding: utf-8 -*-
{
    'name': 'MintDeals Customer API',
    'version': '19.0.1.1.0',
    'category': 'Website',
    'summary': 'JWT-based customer auth and profile API for MintDeals frontend',
    'author': 'MintDeals',
    'website': 'https://mintdeals.com',
    'license': 'LGPL-3',
    'depends': [
        'base',
        'portal',
        'auth_signup',
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
