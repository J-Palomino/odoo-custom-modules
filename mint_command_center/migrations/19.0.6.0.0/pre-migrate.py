"""
Pre-migration for 19.0.6.0.0 — pre-create the new stored columns BEFORE the ORM
loads the models.

Why: ``mint.ptl.deal.format_key`` is a stored computed field. When Odoo first
sees a new stored computed field at upgrade it mass-recomputes it for every
existing row. format_key's compute resolves ``matching_product_ids`` (a
``product.template.search`` over ~62k products) PER DEAL, and there are ~7.6k
deals — so that mass recompute runs ~7.6k product searches inside the upgrade
transaction and never finishes within an HTTP-bound module upgrade (it rolled
back repeatedly on prod 2026-05-29, leaving the column uncreated).

Creating the columns here in pre-migrate means the ORM finds them already
present at load time and does NOT auto-recompute them — the upgrade stays fast.
``x_product_line`` is then backfilled in post-migrate (a single fast SQL UPDATE),
and ``format_key`` is filled in batches afterward by the cron
``ir_cron_fill_format_key`` (see mint.ptl.deal._cron_fill_format_key). Until a
deal's key is filled the storefront uses its ``format_key || name`` fallback, so
behavior is unchanged in the meantime.

Idempotent (ADD COLUMN IF NOT EXISTS); a no-op on fresh installs.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        # Fresh install — the ORM creates the columns; nothing to pre-create.
        return
    cr.execute(
        "ALTER TABLE mint_ptl_deal ADD COLUMN IF NOT EXISTS format_key varchar"
    )
    cr.execute(
        "ALTER TABLE product_template ADD COLUMN IF NOT EXISTS x_product_line varchar"
    )
    _logger.info(
        "mint_command_center 19.0.6.0.0 pre-migrate: pre-created format_key + "
        "x_product_line columns (suppresses the slow upgrade-time mass recompute)"
    )
