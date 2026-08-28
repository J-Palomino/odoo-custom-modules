{
    'name': 'Mint Inventory Operations',
    'version': '19.0.6.0.0',
    'category': 'Inventory',
    'summary': 'Cannabis inventory adjustments, transfers, batch ops, and compliance tracking',
    'description': """Mint Inventory Operations
        Cannabis-specific inventory management built on Odoo stock.

        Adjustments (adjust, combine, convert, move, destroy, discontinue,
        restore) with an approval chain, and Avancir RFID sync.
        NOTE: adjustments update Odoo stock and Avancir ONLY — they do NOT
        reach Dutchie POS, so the register does not see them. The description
        previously claimed otherwise; the model has never contained a single
        Dutchie call.

        Two paths DO reach Dutchie:
          * Receive Inventory — manual delivery intake, validated against
            Dutchie and pushed as a draft or a completed receive.
          * Purchase receipts — validating an incoming transfer linked to a
            PO pushes it to Dutchie POS.
        Both are gated by mint.dutchie_receive.mode (default dry-run) and by
        mintinvsvc's own LocId allowlist.

        REST API at /api/v1/inventory/.""",
    'author': 'Mint Dispensaries',
    'website': 'https://letsgomint.us',
    'license': 'LGPL-3',
    # purchase: the receipt push reads picking.purchase_id and
    # move.purchase_line_id.price_unit to send Dutchie the real landed cost.
    'depends': ['stock', 'product', 'mail', 'purchase', 'mint_api_v2'],
    'data': [
        'security/security_groups.xml',
        'security/ir.model.access.csv',
        'security/record_rules.xml',
        'data/sequence.xml',
        'views/adjustment_views.xml',
        'views/dutchie_refs_views.xml',
        'views/dutchie_receive_views.xml',
        'views/stock_picking_views.xml',
        'views/menu.xml',
    ],
    'installable': True,
    'auto_install': False,
    'application': True,
}
