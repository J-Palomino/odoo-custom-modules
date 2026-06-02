"""Remove the manually-created (RPC / Studio) velocity product-list views
before the module's own xml_id-backed copies load, so the inherited columns
do not double up.

Background: on 2026-06-01 the velocity list + search views were added live via
RPC on prod (DB ir.ui.view, no module xml_id) to surface the F7 fields ahead of
this module change. This module now ships the same two views with proper
xml_ids. Running as a *pre*-migration (before the 19.0.4.18.0 data files load)
guarantees the orphan DB copies are gone first, so there is never a window with
both active. Idempotent: deletes nothing where they were never created
(e.g. staging).
"""
import logging

_logger = logging.getLogger(__name__)

_ORPHAN_VIEW_NAMES = (
    'mcc.product.template.list.velocity',
    'mcc.product.template.search.velocity',
)


def migrate(cr, version):
    cr.execute(
        """
        DELETE FROM ir_ui_view
         WHERE name IN %s
           AND id NOT IN (
               SELECT res_id FROM ir_model_data
                WHERE model = 'ir.ui.view' AND res_id IS NOT NULL)
        """,
        (_ORPHAN_VIEW_NAMES,),
    )
    _logger.info(
        'velocity-view cleanup: removed %s orphan (non-module) DB view(s)',
        cr.rowcount,
    )
