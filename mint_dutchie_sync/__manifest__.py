# -*- coding: utf-8 -*-
{
    'name': 'Mint Dutchie Sync',
    'version': '19.0.3.0.0',
    'application': True,
    'category': 'Sales',
    'summary': 'Dutchie customer sync, home store assignment, purchase tracking, and real-time product sync',
    'author': 'MintDeals',
    'website': 'https://mintdeals.com',
    'license': 'LGPL-3',
    'depends': [
        'base',
        'loyalty',
        'mail',
        'product',
    ],
    'data': [
        'security/ir.model.access.csv',
        'security/dutchie_security.xml',
        'data/loyalty_program.xml',
        'views/dutchie_purchase_views.xml',
        'views/res_partner_views.xml',
        'views/dutchie_menus.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
