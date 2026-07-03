{
    'name': 'MintDeals Embeds',
    'version': '19.0.1.2.1',
    'category': 'Website',
    'summary': 'Embeddable widgets for WordPress and third-party sites',
    'description': """MintDeals Embeds
        Create embeddable widgets (banners, blog feeds, forms, info pages)
        that can be dropped onto WordPress or any external site via a
        single <script> tag.
        Serves the widget JS at /embed/mint-widget.js and provides
        API endpoints for contact forms and newsletter signups.""",
    'author': 'MintDeals',
    'website': 'https://letsgomint.us',
    'license': 'LGPL-3',
    'depends': ['base', 'mint_banner'],
    'data': [
        'security/ir.model.access.csv',
        'security/multicompany_rules.xml',
        'views/embed_views.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
