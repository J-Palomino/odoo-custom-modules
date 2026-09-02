{
    'name': 'MintDeals Gift Cards',
    'version': '19.0.1.0.0',
    'category': 'Sales',
    'summary': 'Stored-value gift cards with partial redemption and a remainder',
    'description': """MintDeals Gift Cards
        A dollar balance that survives partial redemption — which a Dutchie
        discount cannot express, since a $100 code spent on a $30 basket
        destroys the other $70.

        This module is the LEDGER only. It holds balances, takes holds,
        settles them and releases them. It never contacts Dutchie or a
        register: minting the per-draw child coupon and applying it to a live
        basket is the draw engine, built separately on top of this API.

        Promotional and comp credit only. Cards are not sold for money — a
        purchased gift card is deferred revenue, and every draw here books as
        a discount, so the GL would never see the liability.
    """,
    'author': 'MintDeals',
    'website': 'https://letsgomint.us',
    'license': 'LGPL-3',
    'depends': ['base', 'mail', 'mint_api_v2'],
    'data': [
        'security/security_groups.xml',
        'security/ir.model.access.csv',
        'security/record_rules.xml',
        # Wizard actions are referenced by buttons in the card views, so they
        # must be defined first — a %(action_...)d against a record Odoo has
        # not loaded yet fails the whole module install.
        'wizard/gift_card_wizard_views.xml',
        'views/gift_card_views.xml',
        'data/ir_cron.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
