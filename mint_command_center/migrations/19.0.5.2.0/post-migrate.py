"""Backfill mint.deal.submission.option for legacy submissions.

Phase 3 introduces mint.deal.submission.option as the source of truth
for discount specs on a submission; discount_type / discount_value on
the parent become computed-stored mirrors of the first option.

Recipe is shared with the ptl.deal backfill — see migration_helpers.
"""
from odoo.addons.mint_command_center.migration_helpers import backfill_primary_option


def migrate(cr, version):
    backfill_primary_option(
        cr, version,
        parent_table='mint_deal_submission',
        child_table='mint_deal_submission_option',
        fk_col='submission_id',
        host_model='mint.deal.submission',
        option_model='mint.deal.submission.option',
        logger_name='19.0.5.2.0',
    )
