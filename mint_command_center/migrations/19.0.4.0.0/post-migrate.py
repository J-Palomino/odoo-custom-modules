"""
Post-migration for 19.0.4.0.0 — force eager recompute of `is_active`.

The override in mint_discount_ext.py makes `is_active` a stored compute
(driven by is_published + valid window + dow + start/end_time). Existing
rows have the legacy Boolean value still in the column; we force the
ORM to recompute so the stored values reflect the new semantics.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    from odoo import api, SUPERUSER_ID

    env = api.Environment(cr, SUPERUSER_ID, {})
    Discount = env['mint.discount']

    # Materialize the compute eagerly so the first storefront request after
    # deploy doesn't take the recompute hit.
    all_records = Discount.search([])
    if not all_records:
        _logger.info('mint_command_center 19.0.4.0.0 post-migrate: no discounts found')
        return

    _logger.info(
        'mint_command_center 19.0.4.0.0 post-migrate: recomputing is_active for %d discounts',
        len(all_records),
    )
    all_records.invalidate_recordset(['is_active'])
    # Force a read so the stored compute materializes
    _ = [r.is_active for r in all_records]
    _logger.info('mint_command_center 19.0.4.0.0 post-migrate: complete')
