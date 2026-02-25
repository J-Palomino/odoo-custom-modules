{
    'name': 'MintDeals Push Notifications',
    'version': '19.0.1.0.0',
    'category': 'Website',
    'summary': 'Web Push notification support for MintDeals PWA',
    'description': """MintDeals Push Notifications
        Stores browser push subscriptions and sends Web Push notifications
        via VAPID/pywebpush. Exposes REST API at /api/v1/push/.""",
    'author': 'MintDeals',
    'website': 'https://letsgomint.us',
    'license': 'LGPL-3',
    'depends': ['base'],
    'data': ['security/ir.model.access.csv'],
    'external_dependencies': {'python': ['pywebpush']},
    'installable': True,
    'application': False,
    'auto_install': False,
}
