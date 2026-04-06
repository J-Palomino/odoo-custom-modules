"""Set maintenance_team_id on existing equipment records.

The XML data files were created with noupdate=1 but missing the
maintenance_team_id field, so equipment lookups by team returned
nothing — causing all form category dropdowns to be empty.

This migration patches existing records so the fix takes effect
on already-installed instances.
"""
import logging

_logger = logging.getLogger(__name__)

EQUIPMENT_TEAM_MAP = {
    # Facilities equipment → Internal Maintenance team (id=1)
    "mint_maintenance_form.equip_facilities_hvac": 1,
    "mint_maintenance_form.equip_facilities_plumbing": 1,
    "mint_maintenance_form.equip_facilities_electrical": 1,
    "mint_maintenance_form.equip_facilities_janitorial": 1,
    "mint_maintenance_form.equip_facilities_building_access": 1,
    "mint_maintenance_form.equip_facilities_parking": 1,
    "mint_maintenance_form.equip_facilities_signage": 1,
    "mint_maintenance_form.equip_facilities_furniture": 1,
    # Graphics equipment → Graphics team (id=3)
    "mint_maintenance_form.equip_graphics_in_store_signage": 3,
    "mint_maintenance_form.equip_graphics_digital_menu_boards": 3,
    "mint_maintenance_form.equip_graphics_print_materials": 3,
    "mint_maintenance_form.equip_graphics_vehicle_wraps": 3,
    "mint_maintenance_form.equip_graphics_social_digital": 3,
    "mint_maintenance_form.equip_graphics_window_graphics": 3,
    "mint_maintenance_form.equip_graphics_banners_outdoor": 3,
    "mint_maintenance_form.equip_graphics_apparel": 3,
}


def migrate(cr, version):
    if not version:
        return

    _logger.info("Setting maintenance_team_id on existing equipment records...")
    updated = 0

    for xml_id, team_id in EQUIPMENT_TEAM_MAP.items():
        module, name = xml_id.split(".")
        cr.execute(
            """
            UPDATE maintenance_equipment e
            SET maintenance_team_id = %s
            FROM ir_model_data d
            WHERE d.module = %s
              AND d.name = %s
              AND d.model = 'maintenance.equipment'
              AND d.res_id = e.id
              AND (e.maintenance_team_id IS NULL OR e.maintenance_team_id != %s)
            """,
            (team_id, module, name, team_id),
        )
        updated += cr.rowcount

    _logger.info("Updated maintenance_team_id on %d equipment records.", updated)
