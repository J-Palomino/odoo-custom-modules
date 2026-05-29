"""Pre-migration for 19.0.4.15.0 — PTL deal mirror fields for #93723.

Additive only. Adds inclusions, product_ids, is_holiday, event_name on
mint.ptl.deal so submissions can carry these forward via
mint.deal.submission.action_convert_to_deal.

No data migration required — new columns default null/False/[].
Existing PTL rows continue to render their details_exclusions textarea
as before (now relabeled "Exclusions"; the data is unchanged).
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    _logger.info(
        "mint_command_center 19.0.4.15.0: PTL deal mirror fields added "
        "(inclusions, product_ids, is_holiday, event_name). No data migration."
    )
