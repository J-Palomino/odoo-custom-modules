{
    'name': 'Mint Command Center',
    'version': '19.0.1.0.0',
    'category': 'Operations',
    'summary': 'Centralized operations dashboard for Mint Dispensaries',
    'description': """Mint Command Center
    Push notification composer, campaign history, and operations tools
    for Mint Dispensaries staff.""",
    'author': 'MintDeals',
    'website': 'https://letsgomint.us',
    'license': 'LGPL-3',
    'depends': ['base', 'mail', 'mint_push'],
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',
        'views/push_send_wizard_views.xml',
        'views/push_campaign_views.xml',
        'views/menu.xml',
    ],
    'installable': True,
    'application': True,
    'auto_install': False,
}
