# -*- coding: utf-8 -*-
{
    'name': 'Mint Green Theme',
    'version': '19.0.1.0.1',
    'category': 'Themes',
    'summary': 'Changes Odoo UI color from purple to mint green',
    'description': """
        Custom theme module that changes the default Odoo purple accent color
        to mint green for MintDeals branding.
    """,
    'author': 'MintDeals',
    'website': 'https://mintdeals.com',
    'license': 'LGPL-3',
    'depends': ['web'],
    'data': [],
    'assets': {
        'web.assets_backend': [
            'mint_theme/static/src/scss/mint_theme.scss',
        ],
        'web.assets_frontend': [
            'mint_theme/static/src/scss/mint_theme.scss',
        ],
    },
    'installable': True,
    'auto_install': False,
    'application': False,
}
