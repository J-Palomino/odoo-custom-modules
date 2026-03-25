{
    'name': 'Mint TV',
    'version': '19.0.1.0.0',
    'category': 'Marketing',
    'summary': 'In-store TV display management for MintMedia',
    'description': """
        Manage multi-display TV layouts and interactive book viewers
        from Odoo. Configs sync to MintMedia (NodeCG) for rendering.

        Models:
          - TV Layout: Grid-based image/video split across 1–16 screens
          - TV Book: Swipeable page viewer for menus, catalogs, info

        Each record is a display config. Create as many as needed.
        Attach media from Odoo, and MintMedia pulls configs via API.
    """,
    'author': 'Mint Dispensaries',
    'website': 'https://mintdispensaries.com',
    'license': 'LGPL-3',
    'depends': ['mail'],
    'data': [
        'security/ir.model.access.csv',
        'views/tv_layout_views.xml',
        'views/tv_book_views.xml',
        'views/tv_kiosk_views.xml',
        'views/menu.xml',
    ],
    'installable': True,
    'auto_install': False,
    'application': True,
}
