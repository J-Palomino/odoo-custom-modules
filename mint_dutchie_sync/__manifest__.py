# -*- coding: utf-8 -*-
{
    'name': 'Mint Dutchie Sync',
    'version': '19.0.1.0.0',
    'category': 'Sales',
    'summary': 'Dutchie customer sync, home store assignment, and purchase tracking',
    'author': 'MintDeals',
    'website': 'https://mintdeals.com',
    'license': 'LGPL-3',
    'depends': [
        'base',
        'loyalty',
        'mail',
    ],
    'data': [
        'security/ir.model.access.csv',
        'security/dutchie_security.xml',
        'data/loyalty_program.xml',
        'views/res_partner_views.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
