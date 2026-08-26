{
    'name': 'Mint Dutchie Discount Mirror',
    'version': '19.0.1.4.0',
    'category': 'Operations',
    'summary': 'Mirror Dutchie Backoffice discount structure onto mint.discount',
    'description': """
        Extend mint.discount with the fields needed to faithfully mirror
        Dutchie Backoffice discount records. Populated by an external sync
        worker that pulls v2/discount/get-discount + get-discount-by-id and
        upserts by dutchie_discount_id.

        Adds ~25 fields covering:
          - Daily time window (start_time, end_time)
          - Reward calculation detail (calc method, raw value, thresholds)
          - Application / stacking / approval flags
          - Redemption limits
          - Additional restriction types (inventory tags, weight, customer type,
            order type, platform, payment)
          - Display / external metadata (online name, menu rank, external id)
          - Brand-funded promo tracking
    """,
    'author': 'Mint Dispensaries',
    'website': 'https://letsgomint.us',
    'license': 'LGPL-3',
    'depends': [
        'mint_api_v2',
        'mint_command_center',
    ],
    'data': [
        'views/mint_discount_dutchie_views.xml',
    ],
    'installable': True,
    'auto_install': False,
    'application': False,
}
