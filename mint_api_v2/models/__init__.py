# -*- coding: utf-8 -*-
from . import discount_canonical  # plain constants module (no model) — loads + validates the canonical JSON
from . import res_company
from . import product_template
from . import mint_brand_market_banner  # per-market deal banners (mint.brand.market.banner)
from . import mint_strain  # mint.strain master (phase 1: no product.template link)
from . import mint_discount_weight
from . import mint_discount
from . import mint_discount_deal_photo  # deal card photo override (after mint_discount: _inherit)
from . import mint_blog
from . import mint_event
from . import mint_config
from . import mintle_schedule  # extends mint.config with the MINTLE spreadsheet sync
from . import mint_spin_prize
from . import mint_spin_ticket
