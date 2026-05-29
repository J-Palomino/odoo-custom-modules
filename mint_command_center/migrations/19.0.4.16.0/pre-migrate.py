"""Pre-migration for 19.0.4.16.0 — controller updates for #93723.

Controller and JSON-endpoint additions only. No schema changes; no data
migration required.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    _logger.info(
        "mint_command_center 19.0.4.16.0: controller updates for /promos "
        "(new POST keys parsed, /promos/products + /promos/conflicts JSON "
        "endpoints registered). No data migration."
    )
