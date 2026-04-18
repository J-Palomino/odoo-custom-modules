# -*- coding: utf-8 -*-
{
    'name': 'Mint Redis Push',
    'version': '19.0.1.0.0',
    'category': 'Operations',
    'summary': 'Push discount + product-override changes from Odoo to the mintinvsvc Redis cache',
    'description': """
Odoo → Redis push pipeline (replaces the Dutchie-direct-to-Redis path for
discounts; adds a product-override channel for per-product price/name/image
overrides authored in Odoo).

Architecture:
  - mint.redis.push.queue: debounced queue (~20s) processed by cron every 30s
  - mint.discount on-change hooks enqueue per-store pushes
  - product.template on-change hooks (for overrideable fields: list_price,
    rec_price, med_price, name, image_1920) enqueue per-product overrides
  - Hourly full-resync cron repairs drift from missed writes

Config params (in Settings > Technical > Parameters > System Parameters):
  mint_redis_push.url      : mintinvsvc base URL (default production)
  mint_redis_push.api_key  : mintinvsvc REDIS_API_KEY
  mint_redis_push.enabled  : master kill-switch (default True)
    """,
    'author': 'MintDeals',
    'website': 'https://mintdeals.com',
    'license': 'LGPL-3',
    'depends': [
        'base',
        'product',
        'mint_api_v2',
        'mint_command_center',
        'mint_dutchie_discount_mirror',
    ],
    'data': [
        'security/ir.model.access.csv',
        'data/ir_cron.xml',
        'data/ir_config_parameter.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
