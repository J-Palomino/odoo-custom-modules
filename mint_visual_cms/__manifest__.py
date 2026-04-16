{
    'name': 'MintDeals Visual CMS',
    'version': '19.0.1.3.0',
    'category': 'Website',
    'summary': 'Visual wireframe-based banner & image management for marketing',
    'description': """MintDeals Visual CMS
        Interactive wireframe of the store homepage inside Odoo.
        Marketing clicks on visual zones to manage banners and images.
        Routes: /visual-cms, /visual-cms/<slug>""",
    'author': 'MintDeals',
    'website': 'https://letsgomint.us',
    'license': 'LGPL-3',
    'depends': ['website', 'mint_banner'],
    'data': [
        'views/templates.xml',
        'views/menu.xml',
    ],
    'assets': {
        'web.assets_frontend': [
            'mint_visual_cms/static/src/css/wireframe.css',
            'mint_visual_cms/static/src/js/wireframe.js',
        ],
    },
    'installable': True,
    'application': False,
    'auto_install': False,
}
