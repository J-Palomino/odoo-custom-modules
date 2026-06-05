"""Shared helper for option-backfill post-migrate scripts.

Phases 2 and 3 each shipped a near-identical post-migrate that backfills
one mint.discount.option child per legacy host (ptl.deal / deal.submission)
with a non-empty discount_type. Only the table and model names differ.

This helper owns the recipe so a future option-carrying host (hotbox,
brand_calendar, etc.) gets the same migration shape — and so the
SQL-first-then-ORM trick (mandatory to dodge the stored-computed-field
clobber at module load) lives in exactly one place.
"""
import logging


def backfill_primary_option(cr, version, *,
                            parent_table, child_table, fk_col,
                            host_model, option_model,
                            logger_name):
    """Backfill one option per legacy host row.

    Reads pre-upgrade discount_type / discount_value via raw SQL BEFORE
    invoking the ORM — the new stored-computed mirror is initialized at
    module load with option_ids empty, so a normal Host.search would see
    post-init zeroes and the backfill would silently no-op.

    Excludes discount_type IS NULL and empty-string rows (the latter
    isn't a valid Selection member and would abort Option.create on the
    required field — the vendor controller historically stored '' for
    blank).

    Idempotent via NOT EXISTS — re-running an upgrade finds no rows to
    backfill once options are populated.

    Args:
        cr: db cursor (passed by Odoo migration runner).
        version: migration version (early-out on fresh install when None).
        parent_table: SQL table name of the host (e.g. 'mint_ptl_deal').
        child_table: SQL table name of the option (e.g. 'mint_ptl_deal_option').
        fk_col: option column referencing the host (e.g. 'ptl_deal_id').
        host_model: Odoo model name (e.g. 'mint.ptl.deal').
        option_model: Odoo model name (e.g. 'mint.ptl.deal.option').
        logger_name: short tag for log lines (e.g. '19.0.5.1.0').
    """
    if not version:
        return

    _logger = logging.getLogger(__name__)

    cr.execute(f"""
        SELECT h.id, h.discount_type, h.discount_value
        FROM {parent_table} h
        WHERE h.discount_type IS NOT NULL
          AND h.discount_type <> ''
          AND NOT EXISTS (
            SELECT 1
            FROM {child_table} o
            WHERE o.{fk_col} = h.id
          )
    """)
    rows = cr.fetchall()

    if not rows:
        _logger.info(
            'mint_command_center %s post-migrate: no %s rows need option backfill',
            logger_name, host_model,
        )
        return

    _logger.info(
        'mint_command_center %s post-migrate: backfilling %d %s records with an initial option',
        logger_name, len(rows), host_model,
    )

    from odoo import api, SUPERUSER_ID
    env = api.Environment(cr, SUPERUSER_ID, {})
    Host = env[host_model]
    Option = env[option_model]

    # _primary_option_vals lives on the DiscountOptionHostMixin and reads
    # _option_fk_field off the concrete host — every concrete host knows
    # how to shape its own option create-dict, so we don't hard-code the
    # field name here.
    vals_list = [
        Host.browse(host_id)._primary_option_vals(discount_type, discount_value)
        for host_id, discount_type, discount_value in rows
    ]
    Option.create(vals_list)
    _logger.info('mint_command_center %s post-migrate: backfill complete', logger_name)
