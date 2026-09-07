# -*- coding: utf-8 -*-
"""Pre-migration for 19.0.5.10.0 — deadlock-safe add of mint_pos_order.x_receipt_printed.

Why this exists (2026-09-07 incident):
`mint_pos_order` is written continuously by the live Dutchie bulk-sync. When the
stored boolean `x_receipt_printed` was first added, the ORM's plain
`ALTER TABLE ... ADD COLUMN` during `-u` had to take a brief ACCESS EXCLUSIVE
lock, collided with those writes, and lost a Postgres deadlock — the whole `-u`
rolled back, so the field stayed in the registry with NO column and every read of
the model then threw `column ... does not exist`, degrading order intake.

This runs BEFORE the module's ORM schema sync, on Odoo's own upgrade cursor, and
creates the column itself under a short `lock_timeout` with savepoint-scoped
retries. Each attempt either grabs the (metadata-only, PG11+) lock in a gap
between sync batches or times out cleanly and retries — it can never wait long
enough to become a deadlock victim. Once the column exists the ORM's later
`ADD COLUMN` is a no-op, so no lock is taken under contention at all. Idempotent
and re-run safe (an earlier partial `-u` that already added it just returns).

The field is intentionally NOT indexed (see models/pos_order.py) so there is no
second, slower CREATE INDEX lock to contend for.
"""
import logging
import time

_logger = logging.getLogger(__name__)

TABLE = 'mint_pos_order'
COLUMN = 'x_receipt_printed'
MAX_ATTEMPTS = 60
LOCK_TIMEOUT = '1s'      # < deadlock_timeout: prefer a clean timeout over a deadlock
SLEEP_BETWEEN = 2.0      # seconds; ~MAX_ATTEMPTS*(lock_timeout+sleep) worst case


def migrate(cr, version):
    # Fresh installs have no prior `version`; the ORM creates the column on an
    # empty (uncontended) table, so there is nothing to protect against here.
    if not version:
        return

    cr.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = %s AND column_name = %s",
        (TABLE, COLUMN),
    )
    if cr.fetchone():
        _logger.info("%s.%s already present; nothing to do", TABLE, COLUMN)
        return

    last_err = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            cr.execute("SAVEPOINT add_%s" % COLUMN)
            cr.execute("SET LOCAL lock_timeout = %s", (LOCK_TIMEOUT,))
            # constant DEFAULT false => metadata-only in PG11+, no table rewrite;
            # existing rows read as "not printed", which is what we want.
            cr.execute(
                "ALTER TABLE %s ADD COLUMN IF NOT EXISTS %s boolean DEFAULT false"
                % (TABLE, COLUMN)
            )
            cr.execute("RELEASE SAVEPOINT add_%s" % COLUMN)
            _logger.info("added %s.%s on attempt %d/%d",
                         TABLE, COLUMN, attempt, MAX_ATTEMPTS)
            return
        except Exception as e:  # noqa: BLE001 - lock timeout / deadlock / etc.
            last_err = e
            # The failed statement aborts to the savepoint; undo it and retry.
            cr.execute("ROLLBACK TO SAVEPOINT add_%s" % COLUMN)
            _logger.warning(
                "attempt %d/%d to add %s.%s hit %s; retrying in %ss",
                attempt, MAX_ATTEMPTS, TABLE, COLUMN,
                type(e).__name__, SLEEP_BETWEEN,
            )
            time.sleep(SLEEP_BETWEEN)

    # Give up gracefully rather than raising: the ORM will still try its own
    # ADD COLUMN (may succeed in a calmer moment), and if the -u fails the next
    # boot's -u re-runs this idempotently. Never take the site down from here.
    _logger.error(
        "could not add %s.%s after %d attempts (last: %r); "
        "leaving it to the ORM / next upgrade",
        TABLE, COLUMN, MAX_ATTEMPTS, last_err,
    )
