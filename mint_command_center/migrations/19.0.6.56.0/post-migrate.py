"""
Post-migration for 19.0.6.56.0 — one-time catch-up that promotes legacy
preferred_start_date / preferred_end_date into structured mint.deal.submission
Plot Windows for every submission created since the original 19.0.4.14.0
backfill (the public vendor form keeps writing the legacy fields, and that
version-pinned migration only ran once).

Reuses mint.deal.submission._sync_legacy_window via the ORM (not raw SQL) so
the stored windows_summary / run_end_date recompute as the windows are created.
Idempotent — rows that already have windows are skipped by the helper.

`preferred_days` (free-text, e.g. "Mon, Wed, Fri") is intentionally NOT parsed
— too lossy. It stays on the legacy field for staff reference.
"""
import logging

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    env = api.Environment(cr, SUPERUSER_ID, {})
    subs = env['mint.deal.submission'].search([
        '|',
        ('preferred_start_date', '!=', False),
        ('preferred_end_date', '!=', False),
        ('window_ids', '=', False),
    ])
    before = len(subs)
    subs._sync_legacy_window()
    promoted = sum(1 for s in subs if s.window_ids)
    _logger.info(
        "mint_command_center 19.0.6.56.0: promoted %s/%s legacy-only "
        "submission(s) to Plot Windows",
        promoted, before,
    )
