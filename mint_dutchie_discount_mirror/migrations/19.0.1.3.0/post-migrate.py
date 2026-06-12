# -*- coding: utf-8 -*-
"""De-mislabel mint.discount.discount_type from the authoritative calc id.

Before 2026-06-05 discount_type was stored INVERTED relative to
calculation_method_id (live-verified on prod): calc-1 rows were labeled
'percent' (should be 'fixed'), calc-2 'fixed' (should be 'percent'), calc-15
'bundle' (should be 'points_multiplier'). ~817 rows affected (759 calc-2 +
51 calc-1 + 7 calc-15) plus any calc-3/5/6 that already matched.

Data-only and idempotent: only rows whose discount_type DIFFERS from the
canonical value for their calc id are touched. The mapping is read from the
shared canonical registry so it can't drift from the runtime code.

Note: this does NOT re-push to Redis (no network in migrations). The Redis
emit already keys off calculation_method_id (not discount_type) after the
serializer fix, so storefront pricing is corrected by the next sync /
action_push_all_stores_to_redis. This migration is Odoo-internal hygiene
(reporting, the discount form, and future pushes reading discount_type).
"""
import logging

from odoo.addons.mint_api_v2.models.discount_canonical import ODOO_TYPE_BY_ID

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    total = 0
    for cmid, odoo_type in ODOO_TYPE_BY_ID.items():
        cr.execute(
            """
            UPDATE mint_discount
               SET discount_type = %s
             WHERE calculation_method_id = %s
               AND discount_type IS DISTINCT FROM %s
            """,
            (odoo_type, cmid, odoo_type),
        )
        if cr.rowcount:
            _logger.info(
                "discount_type de-mislabel: %s row(s) calculation_method_id=%s -> %s",
                cr.rowcount, cmid, odoo_type,
            )
            total += cr.rowcount
    _logger.info("discount_type de-mislabel: %s row(s) corrected total", total)
