"""
Pre-migration for 19.0.4.5.3 — add new integer columns to res_company before
the registry-level upgrade reads them.

Adds `dutchie_pos_location_id` + `dutchie_lsp_id` columns up-front so the
upgrade flow doesn't error with "column res_company.dutchie_pos_location_id
does not exist" the first time something reads res.company after the new
Python field declarations land but before _auto_init has had a chance to
ALTER TABLE (this race was observed in prod 2026-05-19).

Both columns are nullable INTEGER, default NULL — no values populated here.
Ops backfills per store via `res.company.write({dutchie_pos_location_id: ...})`
after the upgrade lands.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    cr.execute("""
        ALTER TABLE res_company
        ADD COLUMN IF NOT EXISTS dutchie_pos_location_id INTEGER
    """)
    cr.execute("""
        ALTER TABLE res_company
        ADD COLUMN IF NOT EXISTS dutchie_lsp_id INTEGER
    """)
    _logger.info('[19.0.4.5.3] ensured res_company.dutchie_pos_location_id + dutchie_lsp_id columns exist')
