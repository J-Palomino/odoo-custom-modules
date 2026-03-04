{
    'name': 'MintDeals Banners',
    'version': '19.0.1.1.0',
    'category': 'Website',
    'summary': 'Admin-managed hero & category banners for MintDeals storefront',
    'description': """MintDeals Banners
        Manage hero carousel and category ad banners for store pages.
        Slots: hero, after-flower, after-vapes, after-edibles, after-concentrates.
        Exposes REST API at /api/v1/banners.
        Images served via /web/image/ URLs (S3-backed) instead of inline base64.""",
    'author': 'MintDeals',
    'website': 'https://letsgomint.us',
    'license': 'LGPL-3',
    'depends': ['base', 'mint_command_center'],
    'data': [
        'security/ir.model.access.csv',
        'views/banner_views.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
