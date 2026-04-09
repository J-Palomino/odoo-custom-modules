# -*- coding: utf-8 -*-
"""Deactivate conflicting stock.location record rule that blocks
company_id=False locations (Vendors, Customers, Transit) when company 1
is not in the user's active company context.

Rule 667 was created via UI on 2026-02-17 and conflicts with standard
Odoo rule 112 which correctly includes False company locations.  The AND
of both rules causes AccessError during receiving operations for users
who don't have company 1 selected in their company switcher.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute("""
        UPDATE ir_rule
        SET active = false
        WHERE model_id = (SELECT id FROM ir_model WHERE model = 'stock.location')
          AND domain_force ILIKE '%%if 1 in company_ids%%'
          AND active = true
    """)
    if cr.rowcount:
        _logger.info("Deactivated %d conflicting stock.location record rule(s)", cr.rowcount)
    else:
        _logger.info("No conflicting stock.location record rules found to deactivate")
