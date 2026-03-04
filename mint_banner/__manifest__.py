{
    'name': 'MintDeals Banners',
    'version': '19.0.1.2.0',
    'category': 'Website',
    'summary': 'Admin-managed category banners for MintDeals storefront',
    'description': """MintDeals Banners
        Manage interstitial and slot banners for category pages.
        Exposes REST API at /api/v1/banners.""",
    'author': 'MintDeals',
    'website': 'https://letsgomint.us',
    'license': 'LGPL-3',
    'depends': ['base'],
    'data': [
        'security/ir.model.access.csv',
        'views/banner_views.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
