# -*- coding: utf-8 -*-
{
    'name': 'Mint OAuth Only Login',
    'version': '19.0.1.0.0',
    'category': 'Authentication',
    'summary': 'Hide password form on login page — Google OAuth only',
    'description': """
        Hides the username/password login form and shows only the
        Google OAuth sign-in button on the Odoo login page.
    """,
    'author': 'MintDeals',
    'website': 'https://mintdeals.com',
    'license': 'LGPL-3',
    'depends': [
        'web',
        'auth_oauth',
    ],
    'data': [
        'views/login_templates.xml',
    ],
    'assets': {
        'web.assets_frontend': [
            'mint_oauth_only/static/src/css/login.css',
        ],
    },
    'installable': True,
    'application': False,
    'auto_install': False,
}
