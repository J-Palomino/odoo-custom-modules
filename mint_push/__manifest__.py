{
    'name': 'MintDeals Push Notifications',
    'version': '19.0.1.3.0',
    'category': 'Website',
    'summary': 'Web Push notification support for MintDeals PWA',
    'description': """MintDeals Push Notifications
        Stores browser push subscriptions and sends Web Push notifications
        via VAPID/pywebpush. Exposes REST API at /api/v1/push/.""",
    'author': 'MintDeals',
    'website': 'https://letsgomint.us',
    'license': 'LGPL-3',
    'depends': ['base', 'website'],
    'data': [
        'security/ir.model.access.csv',
        'data/push_site_data.xml',
        'wizard/push_send_wizard_views.xml',
        'views/push_subscription_views.xml',
        'views/push_site_views.xml',
    ],
    'external_dependencies': {'python': ['pywebpush', 'py_vapid']},
    'installable': True,
    'application': False,
    'auto_install': False,
}
