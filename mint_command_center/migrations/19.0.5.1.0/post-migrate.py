"""Backfill mint.ptl.deal.option for legacy deals.

Phase 1 (19.0.5.0.0) introduced mint.ptl.deal.option as an additive child.
Phase 2 (this version) makes mint.ptl.deal.discount_type/discount_value
computed mirrors of the first option, so every existing deal needs at
least one option row to keep the mirror correct.

Recipe is shared with the deal.submission backfill — see migration_helpers.
"""
from odoo.addons.mint_command_center.migration_helpers import backfill_primary_option


def migrate(cr, version):
    backfill_primary_option(
        cr, version,
        parent_table='mint_ptl_deal',
        child_table='mint_ptl_deal_option',
        fk_col='ptl_deal_id',
        host_model='mint.ptl.deal',
        option_model='mint.ptl.deal.option',
        logger_name='19.0.5.1.0',
    )
