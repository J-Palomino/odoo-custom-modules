import logging

_logger = logging.getLogger(__name__)

# Fix-It form equipment is grouped by xml_id prefix; each group belongs to the
# maintenance team of the given NAME. Resolving by name (not a hard-coded id)
# keeps the assignment correct across fresh / restored / cloned DBs where the
# team ids differ. (The old 19.0.9.0.0 migration hard-coded ids — e.g. Graphics
# as 3 — which is already wrong on prod where Graphics is a different id.)
_EQUIPMENT_TEAM_BY_NAME = [
    ("equip_facilities_", "Internal Maintenance"),
    ("equip_graphics_", "Graphics"),
    ("equip_eng_", "Engineering"),
]


def post_init_hook(env):
    """Backfill maintenance_team_id on the Fix-It form equipment BY TEAM NAME.

    Runs on install. Idempotent and conservative:
      * only sets equipment whose team is currently UNSET — never overwrites an
        existing assignment, so it can't disturb already-configured prod data;
      * a missing team is logged and skipped — never raises, so it can't abort
        the install.
    """
    Team = env["maintenance.team"].sudo()
    Equipment = env["maintenance.equipment"].sudo()
    IMD = env["ir.model.data"].sudo()

    for prefix, team_name in _EQUIPMENT_TEAM_BY_NAME:
        team = Team.search([("name", "=", team_name)], limit=1)
        if not team:
            _logger.warning(
                "mint_maintenance_form: maintenance team %r not found; "
                "left %s* equipment unassigned", team_name, prefix)
            continue
        data = IMD.search([
            ("module", "=", "mint_maintenance_form"),
            ("model", "=", "maintenance.equipment"),
            ("name", "=like", prefix + "%"),
        ])
        equipment = Equipment.browse(data.mapped("res_id")).exists()
        to_set = equipment.filtered(lambda e: not e.maintenance_team_id)
        if to_set:
            to_set.write({"maintenance_team_id": team.id})
            _logger.info(
                "mint_maintenance_form: assigned team %r (id %s) to %d %s* equipment",
                team_name, team.id, len(to_set), prefix)
