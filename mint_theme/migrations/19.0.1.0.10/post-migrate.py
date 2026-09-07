# -*- coding: utf-8 -*-
"""Deactivate dangling ``/_custom/`` ir.asset records whose attachment is gone.

``ir.asset`` 418 "Mint brand primary variables" was created through the UI on
2026-02-16 (no ``ir.model.data`` xmlid) with ``directive='append'`` pointing at::

    /_custom/web._assets_primary_variables/mint_theme/primary_variables.scss

The backing ``ir.attachment`` no longer exists -- of 124,188 attachments in
production, zero had a ``_custom`` url.  An ``append`` of unfetchable content is a
hard failure of ``web._assets_primary_variables``, a base bundle included by BOTH
``web.assets_backend`` and ``web.assets_frontend``, so every page in the database
rendered with the banner:

    Style error.  The style compilation failed.  This is an administrator or
    developer error that must be fixed for the entire database before continuing
    working.

Odoo does not log this -- it serves a ~389 byte stub in place of the ~1.3 MB
bundle, carrying the real cause in a ``css_error_message`` rule.

The record is redundant: this module's manifest already appends the genuine
``mint_theme/static/src/scss/primary_variables.scss`` to the same bundle, so the
brand variables ($o-brand-primary: #00954c) are supplied either way.  Fixed live
in production 2026-09-07 19:32 UTC; this migration codifies it so a database
restore or a prod->staging clone cannot bring the outage back.

Deliberately narrow: only ``append`` directives, and only where the attachment is
genuinely absent.  Sibling ``replace`` records that also dangle are left alone --
their ``target`` is not present in that bundle, so the path never resolves and
compilation succeeds with them in place.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute("""
        UPDATE ir_asset
           SET active = false
         WHERE active = true
           AND directive = 'append'
           AND strpos(path, '/_custom/') = 1
           AND NOT EXISTS (
                 SELECT 1 FROM ir_attachment att WHERE att.url = ir_asset.path
               )
    """)
    if cr.rowcount:
        _logger.info(
            "Deactivated %d dangling /_custom/ ir.asset append record(s) "
            "that would break style compilation database-wide", cr.rowcount
        )
    else:
        _logger.info("No dangling /_custom/ ir.asset append records found")
