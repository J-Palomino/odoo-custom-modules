{
    'name': 'Mint Inventory Operations',
    'version': '19.0.3.0.0',
    'category': 'Inventory',
    'summary': 'Cannabis inventory adjustments, transfers, batch ops, and compliance tracking',
    'description': """Mint Inventory Operations
        Cannabis-specific inventory management built on Odoo stock.
        Adjustment workflows (adjust, combine, convert, move, destroy, discontinue, restore)
        with approval chain, Avancir RFID sync, and Dutchie Backoffice sync.
        REST API at /api/v1/inventory/.""",
    'author': 'Mint Dispensaries',
    'website': 'https://letsgomint.us',
    'license': 'LGPL-3',
    'depends': ['stock', 'product', 'mail', 'mint_api_v2'],
    'data': [
        'security/security_groups.xml',
        'security/ir.model.access.csv',
        'security/record_rules.xml',
        'data/sequence.xml',
        'views/adjustment_views.xml',
        'views/dutchie_receive_views.xml',
        'views/menu.xml',
    ],
    'installable': True,
    'auto_install': False,
    'application': True,
}
