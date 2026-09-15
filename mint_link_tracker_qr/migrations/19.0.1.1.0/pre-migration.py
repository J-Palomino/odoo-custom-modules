# Adopt the Link Tracker employee-access records that were created by hand in
# the prod DB on 2026-09-15 (ACL + two write rules, no external id). This
# version ships the same records as module data; without an xmlid the loader
# would create a second copy of each. Bind the existing rows to the new xmlids
# so the data files update them in place. DBs without the hand-made rows
# (staging, fresh installs) are a no-op.

MODULE = "mint_link_tracker_qr"

ADOPT = [
    ("ir.model.access", "ir_model_access", "access_link_tracker_employee",
     "link.tracker employee (all internal users)"),
    ("ir.rule", "ir_rule", "rule_link_tracker_edit_all",
     "Link Tracker: admin/marketing/website/project-admin edit all"),
    ("ir.rule", "ir_rule", "rule_link_tracker_employee_edit_own",
     "Link Tracker: employees edit own links"),
]


def migrate(cr, version):
    if not version:
        return
    for model, table, xmlid, name in ADOPT:
        cr.execute(
            "SELECT 1 FROM ir_model_data WHERE module = %s AND name = %s",
            (MODULE, xmlid),
        )
        if cr.fetchone():
            continue
        cr.execute(
            f"""
            SELECT r.id FROM {table} r
              JOIN ir_model m ON m.id = r.model_id
             WHERE m.model = 'link.tracker'
               AND r.name = %s
               AND NOT EXISTS (
                   SELECT 1 FROM ir_model_data d
                    WHERE d.model = %s AND d.res_id = r.id)
             ORDER BY r.id
             LIMIT 1
            """,
            (name, model),
        )
        row = cr.fetchone()
        if row:
            cr.execute(
                """
                INSERT INTO ir_model_data (module, name, model, res_id, noupdate)
                VALUES (%s, %s, %s, %s, false)
                """,
                (MODULE, xmlid, model, row[0]),
            )
