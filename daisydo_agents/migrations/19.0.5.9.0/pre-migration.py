# Adopt the pre-existing "Daisy Agents" fleet group (created live in the DB
# without an external id) under daisydo_agents.group_daisy_agents, so the
# record declared in security/agent_security.xml updates it in place instead
# of creating a duplicate group.


def migrate(cr, version):
    if not version:
        return
    cr.execute(
        """
        SELECT 1 FROM ir_model_data
        WHERE module = 'daisydo_agents' AND name = 'group_daisy_agents'
        """
    )
    if cr.fetchone():
        return

    # res_groups.name is translated, hence jsonb
    cr.execute(
        "SELECT id FROM res_groups WHERE name->>'en_US' = 'Daisy Agents' ORDER BY id LIMIT 1"
    )
    row = cr.fetchone()
    if row:
        cr.execute(
            """
            INSERT INTO ir_model_data (module, name, model, res_id, noupdate)
            VALUES ('daisydo_agents', 'group_daisy_agents', 'res.groups', %s, false)
            """,
            (row[0],),
        )
