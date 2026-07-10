"""Adopt the ad-hoc region-stamping records into this module.

The region rollup automation was first created over RPC with no external IDs:
  - ir.actions.server  "Set x_state_region/x_store_location from company"
  - base.automation    "Maintenance: set state/locale from company"

This module now ships those records (data/maintenance_region_data.xml) with the
external IDs `action_set_maintenance_region` and `automation_maintenance_region`.
Running BEFORE the new data loads, we bind any pre-existing RPC record to the
matching external ID so the XML UPDATES it in place instead of creating a second
(duplicate) automation. Idempotent, and a no-op on databases that never had the
RPC records (e.g. a fresh install or the staging clone).
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)

MODULE = "mint_maintenance_form"

# (external id name, model, unique match domain for the legacy RPC record)
BINDINGS = [
    (
        "action_set_maintenance_region",
        "ir.actions.server",
        [
            ("name", "=", "Set x_state_region/x_store_location from company"),
            ("usage", "=", "base_automation"),
        ],
    ),
    (
        "automation_maintenance_region",
        "base.automation",
        [("name", "=", "Maintenance: set state/locale from company")],
    ),
]


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    IMD = env["ir.model.data"]
    for name, model, domain in BINDINGS:
        # Already bound (idempotent re-run / fresh install already created it).
        if IMD.search_count([("module", "=", MODULE), ("name", "=", name)]):
            _logger.info("Region adopt: %s.%s already bound, skipping", MODULE, name)
            continue
        recs = env[model].search(domain)
        if not recs:
            _logger.info(
                "Region adopt: no legacy %s matching %s; XML will create it fresh",
                model, domain)
            continue
        if len(recs) > 1:
            _logger.warning(
                "Region adopt: %s legacy %s records match %s; adopting the "
                "oldest (id=%s) and leaving the rest for manual review",
                len(recs), model, domain, min(recs.ids))
            recs = env[model].browse(min(recs.ids))
        # Skip if this record already carries some other external id — don't
        # steal ownership from another module.
        existing = IMD.search([("model", "=", model), ("res_id", "=", recs.id)])
        if existing:
            _logger.warning(
                "Region adopt: %s id=%s already has ext-id %s.%s; not rebinding",
                model, recs.id, existing[0].module, existing[0].name)
            continue
        IMD.create({
            "module": MODULE,
            "name": name,
            "model": model,
            "res_id": recs.id,
            "noupdate": False,
        })
        _logger.info("Region adopt: bound %s.%s -> %s id=%s", MODULE, name, model, recs.id)
