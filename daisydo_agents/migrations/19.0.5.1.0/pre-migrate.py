"""Pre-migration: remove duplicate daisy.agent records by code.

The module is adding a UNIQUE(code) constraint, but production already has
duplicate `code` values (e.g. hugo-odoo).  For each set of duplicates we keep
the row with the lowest id and delete the rest, along with any related records
that would block the DELETE due to foreign-key constraints.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    _logger.info("pre-migrate: scanning daisy_agent for duplicate codes …")

    # Find all code values that appear more than once
    cr.execute("""
        SELECT code, array_agg(id ORDER BY id) AS ids
          FROM daisy_agent
         WHERE code IS NOT NULL
         GROUP BY code
        HAVING COUNT(*) > 1
    """)
    duplicates = cr.fetchall()

    if not duplicates:
        _logger.info("pre-migrate: no duplicate agent codes found — nothing to do")
        return

    all_ids_to_delete = []
    for code, ids in duplicates:
        keep_id = ids[0]
        remove_ids = ids[1:]
        _logger.info(
            "pre-migrate: code=%r — keeping id=%s, deleting ids=%s",
            code, keep_id, remove_ids,
        )
        all_ids_to_delete.extend(remove_ids)

    if not all_ids_to_delete:
        return

    ids_tuple = tuple(all_ids_to_delete)

    # Re-point related feeds to the kept agent before deleting
    # (agent_id is the FK on daisy_agent_feed → daisy_agent)
    cr.execute("""
        SELECT column_name, table_name
          FROM information_schema.columns
         WHERE table_schema = 'public'
           AND column_name = 'agent_id'
           AND table_name IN (
               SELECT tablename FROM pg_tables WHERE schemaname = 'public'
           )
    """)
    fk_tables = cr.fetchall()

    for col, table in fk_tables:
        if table == "daisy_agent":
            continue
        _logger.info("pre-migrate: clearing %s.%s refs for deleted agents", table, col)
        cr.execute(
            f"UPDATE {table} SET {col} = NULL WHERE {col} IN %s",  # noqa: S608
            (ids_tuple,),
        )

    # Delete the duplicate agent records themselves
    cr.execute("DELETE FROM daisy_agent WHERE id IN %s", (ids_tuple,))
    _logger.info(
        "pre-migrate: deleted %d duplicate daisy_agent record(s)",
        cr.rowcount,
    )
