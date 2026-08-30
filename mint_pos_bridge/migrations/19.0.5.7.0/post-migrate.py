# -*- coding: utf-8 -*-
"""Drop the orphaned mint_web_order_config.dutchie_lsp_id column.

The field became a non-stored compute that mirrors res.company via the single
LSP resolver, so the column is no longer read or written. It held a second,
independently-editable copy of "which Dutchie tenant owns this location" — the
kind of duplicate that silently routes POS traffic to the wrong tenant, which
the field's own help text warned about.

Safety: before this landed, all 41 configured rows were compared against
res.company and NONE disagreed on either lsp or loc, so no information is lost
by dropping it. The values now come from res.company.dutchie_lsp_id.

Idempotent — IF EXISTS makes a re-run a no-op.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute("""
        SELECT column_name
          FROM information_schema.columns
         WHERE table_name = 'mint_web_order_config'
           AND column_name = 'dutchie_lsp_id'
    """)
    if not cr.fetchone():
        _logger.info('[19.0.5.7.0] dutchie_lsp_id column already absent — no-op')
        return

    # Log any residual disagreement before dropping, so a surprise is visible
    # in the upgrade log rather than lost.
    cr.execute("""
        SELECT c.id, c.dutchie_lsp_id, co.dutchie_lsp_id
          FROM mint_web_order_config c
          JOIN res_company co ON co.id = c.company_id
         WHERE COALESCE(c.dutchie_lsp_id, 0) <> COALESCE(co.dutchie_lsp_id, 0)
    """)
    drift = cr.fetchall()
    if drift:
        _logger.warning(
            '[19.0.5.7.0] %d web order config row(s) disagreed with their '
            'store LSP and are being dropped in favour of res.company: %s',
            len(drift), drift)

    cr.execute('ALTER TABLE mint_web_order_config DROP COLUMN IF EXISTS dutchie_lsp_id')
    _logger.info('[19.0.5.7.0] dropped mint_web_order_config.dutchie_lsp_id — '
                 'now resolved from res.company via _dutchie_lsp()')
