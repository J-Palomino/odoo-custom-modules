{
    'name': 'MintDeals Gift Cards',
    'version': '19.0.1.4.0',
    'category': 'Sales',
    'summary': 'Stored-value gift cards with partial redemption and a remainder',
    'description': """MintDeals Gift Cards
        A dollar balance that survives partial redemption — which a Dutchie
        discount cannot express, since a $100 code spent on a $30 basket
        destroys the other $70.

        Two layers, deliberately separated:

        * The LEDGER (gift_card.py, gift_card_line.py) holds balances and
          moves money between held / settled / released. It does not know
          Dutchie exists.
        * The DRAW ENGINE (gift_card_draw.py) reads the customer's live
          basket, decides how much to take, and mints a single-use Dutchie
          coupon for exactly that amount. The card's own code is never
          pushed to Dutchie, so it is worthless at a register on its own.

        Promotional and comp credit only. Cards are not sold for money — a
        purchased gift card is deferred revenue, and every draw here books as
        a discount, so the GL would never see the liability.
    """,
    'author': 'MintDeals',
    'website': 'https://letsgomint.us',
    'license': 'LGPL-3',
    # mint_command_center carries the Dutchie push machinery the draw engine
    # reuses (_push_one_discount, _resolve_pos_loc_id, the invsvc URL/key).
    # Depending on it does not widen the upgrade blast radius: a bad view in
    # this module still only rolls back this module.
    # mint_dutchie_discount_mirror contributes the 'dollar_off_total'
    # discount_type the child coupons use — it is NOT in the base selection.
    # mint_customer_api supplies the customer JWT auth + response helpers the
    # storefront endpoints reuse, so a shopper is authenticated exactly the
    # same way here as on every other /api/v1/customer route.
    'depends': [
        'base', 'mail', 'mint_api_v2', 'mint_command_center',
        'mint_dutchie_discount_mirror', 'mint_customer_api',
    ],
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
