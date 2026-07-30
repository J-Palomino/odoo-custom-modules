"""Drop the orphaned manual "Related Requests" view now the module owns it.

x_related_request_ids and its form view were created live against production on
2026-07-28 (ir.model.fields 72625, ir.ui.view 10769) to unblock cross-linking
MR-1137 <-> MR-670/MR-912. 19.0.9.9.0 declares both in the module.

The FIELD needs no work here: it was created with state='manual' under the same
name, so the ORM adopts the existing column and its rows on upgrade — the same
approach already used for x_sdlc_* and x_state_region.

The VIEW does need work. The live one was created without an xml_id, so the
module cannot claim it; loading views/maintenance_request_views.xml simply adds
a SECOND inherited view with the same xpath, and the form renders the field
twice. This drops the orphan.

Matched by content rather than by id 10769 so it is correct on any database and
a no-op where the manual view never existed (staging, a fresh install, or a
re-run of this migration).
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        # Fresh install: the module creates the view itself, nothing to clean.
        return

    # Delete inherited maintenance.request views that render the field but are
    # owned by no module. The view this migration ships has an ir_model_data row
    # (created moments earlier when the data files loaded), so it is preserved.
    cr.execute(
        """
        DELETE FROM ir_ui_view v
        WHERE v.model = 'maintenance.request'
          AND v.arch_db::text LIKE %s
          AND NOT EXISTS (
              SELECT 1
              FROM ir_model_data d
              WHERE d.model = 'ir.ui.view'
                AND d.res_id = v.id
          )
        """,
        ("%x_related_request_ids%",),
    )
    if cr.rowcount:
        _logger.info(
            "Removed %d orphaned manual 'Related Requests' view(s); the module "
            "-owned view now renders x_related_request_ids exactly once.",
            cr.rowcount,
        )
    else:
        _logger.info(
            "No orphaned 'Related Requests' view found — nothing to clean."
        )
