"""Set maintenance_team_id on existing equipment records.

The XML data files were created with noupdate=1 but missing the
maintenance_team_id field, so equipment lookups by team returned
nothing — causing all form category dropdowns to be empty.

This migration patches existing records so the fix takes effect
on already-installed instances.

Robustness fix (2026-06-12): the Graphics team id was hard-coded to 3, but the
Graphics team (`team_graphics`) is id 12 on prod — team 3 does not exist, so the
UPDATE raised a ForeignKeyViolation, rolled back the whole upgrade, and (because
the module version never advanced) re-ran and failed on EVERY subsequent upgrade.
Now the Graphics team id is resolved dynamically from its xml_id, and every UPDATE
is guarded so it can never target a non-existent team.
"""
import logging

_logger = logging.getLogger(__name__)

# Facilities equipment → Internal Maintenance team (id=1).
FACILITIES_EQUIPMENT = [
    "equip_facilities_hvac",
    "equip_facilities_plumbing",
    "equip_facilities_electrical",
    "equip_facilities_janitorial",
    "equip_facilities_building_access",
    "equip_facilities_parking",
    "equip_facilities_signage",
    "equip_facilities_furniture",
]
GRAPHICS_EQUIPMENT = [
    "equip_graphics_in_store_signage",
    "equip_graphics_digital_menu_boards",
    "equip_graphics_print_materials",
    "equip_graphics_vehicle_wraps",
    "equip_graphics_social_digital",
    "equip_graphics_window_graphics",
    "equip_graphics_banners_outdoor",
    "equip_graphics_apparel",
]


def _resolve_team(cr, xml_id_name):
    """Return the maintenance.team id for a mint_maintenance_form xml_id, or None."""
    cr.execute(
        """
        SELECT res_id FROM ir_model_data
        WHERE module = 'mint_maintenance_form' AND name = %s
          AND model = 'maintenance.team'
        """,
        (xml_id_name,),
    )
    row = cr.fetchone()
    return row[0] if row else None


def migrate(cr, version):
    if not version:
        return

    _logger.info("Setting maintenance_team_id on existing equipment records...")

    # Facilities → Internal Maintenance (id=1, base maintenance team).
    # Graphics → the actual Graphics team, resolved from its xml_id (NOT a
    # hard-coded id — that was the bug).
    team_map = {name: 1 for name in FACILITIES_EQUIPMENT}
    graphics_team_id = _resolve_team(cr, "team_graphics")
    if graphics_team_id:
        for name in GRAPHICS_EQUIPMENT:
            team_map[name] = graphics_team_id
    else:
        _logger.warning(
            "team_graphics not found; skipping Graphics equipment team assignment.")

    updated = 0
    for name, team_id in team_map.items():
        # The EXISTS guard makes this a no-op (0 rows) if the target team is
        # missing, instead of raising a ForeignKeyViolation that aborts the
        # whole module upgrade.
        cr.execute(
            """
            UPDATE maintenance_equipment e
            SET maintenance_team_id = %s
            FROM ir_model_data d
            WHERE d.module = 'mint_maintenance_form'
              AND d.name = %s
              AND d.model = 'maintenance.equipment'
              AND d.res_id = e.id
              AND (e.maintenance_team_id IS NULL OR e.maintenance_team_id != %s)
              AND EXISTS (SELECT 1 FROM maintenance_team t WHERE t.id = %s)
            """,
            (team_id, name, team_id, team_id),
        )
        updated += cr.rowcount

    _logger.info("Updated maintenance_team_id on %d equipment records.", updated)
