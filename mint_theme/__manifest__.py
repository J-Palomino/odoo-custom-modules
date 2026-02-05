# -*- coding: utf-8 -*-
{
    'name': 'MinTask Theme',
    'version': '19.0.1.0.5',
    'category': 'Themes',
    'summary': 'Customizable Odoo theme with brand color configuration',
    'description': """
        Customizable theme module for Odoo 19:
        - Replaces default purple accent with configurable brand colors
        - Browser tab title changed to MinTask
        - Easy color customization via SCSS variables
        - Includes preset themes: green, blue, orange, purple, charcoal, teal, rose
        - Use generate-theme.sh to switch themes quickly
    """,
    'author': 'MintDeals',
    'website': 'https://mintdeals.com',
    'license': 'LGPL-3',
    'depends': ['web'],
    'data': [
        'views/templates.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'mint_theme/static/src/scss/mint_theme.scss',
            'mint_theme/static/src/js/mintask.js',
        ],
        'web.assets_frontend': [
            'mint_theme/static/src/scss/mint_theme.scss',
            'mint_theme/static/src/js/mintask.js',
        ],
    },
    'installable': True,
    'auto_install': False,
    'application': False,
}
