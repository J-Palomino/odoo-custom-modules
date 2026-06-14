import logging

_logger = logging.getLogger(__name__)


# Fixes are described as: (table, fk_name, column, ref_table, ondelete, use_not_valid)
# NOT VALID skips the existing-row scan on huge tables (only validates new writes).
# All are idempotent: DROP IF EXISTS then ADD CONSTRAINT.
_FK_FIXES = [
    # mail.followers ↔ mail.message.subtype M2M rel — full validation OK (rel table is moderate)
    ("mail_followers_mail_message_subtype_rel",
     "mail_followers_mail_message_subtype_rel_mail_followers_id_fkey",
     "mail_followers_id", "mail_followers", "CASCADE", False),
    ("mail_followers_mail_message_subtype_rel",
     "mail_followers_mail_message_subtype_rel_mail_message_subtype_id_fkey",
     "mail_message_subtype_id", "mail_message_subtype", "CASCADE", False),
    # mail.tracking.value — large table, use NOT VALID
    ("mail_tracking_value",
     "mail_tracking_value_mail_message_id_fkey",
     "mail_message_id", "mail_message", "CASCADE", True),
    # discuss.channel.member — large enough, use NOT VALID
    ("discuss_channel_member",
     "discuss_channel_member_fetched_message_id_fkey",
     "fetched_message_id", "mail_message", "SET NULL", True),
    ("discuss_channel_member",
     "discuss_channel_member_seen_message_id_fkey",
     "seen_message_id", "mail_message", "SET NULL", True),
]

# Pre-FK orphan cleanup queries — run before adding the FK so the validated
# constraint doesn't fail. Each is (table_label, sql).
_ORPHAN_CLEANUPS = [
    ("mail_followers_rel_followers",
     "DELETE FROM mail_followers_mail_message_subtype_rel "
     "WHERE mail_followers_id NOT IN (SELECT id FROM mail_followers)"),
    ("mail_followers_rel_subtypes",
     "DELETE FROM mail_followers_mail_message_subtype_rel "
     "WHERE mail_message_subtype_id NOT IN (SELECT id FROM mail_message_subtype)"),
    # Defensive: drop mail.notification rows that reference deleted mail.mail
    # (Odoo's ORM expects ondelete='set null' but FK isn't always set up that way.)
    ("mail_notification_broken_fk",
     "DELETE FROM mail_notification "
     "WHERE mail_mail_id IS NOT NULL "
     "  AND mail_mail_id NOT IN (SELECT id FROM mail_mail)"),
]


def post_init_hook(env):
    """Idempotent: re-applies on install/upgrade. Safe to run repeatedly.

    Each statement runs inside its OWN savepoint and is guarded: if a core mail
    table/column gets renamed or removed in an Odoo point release, that single
    statement is rolled back and logged, and the hook keeps going. Without this,
    one schema drift here would raise and abort the entire upgrade transaction —
    blocking not just this module but every module batched in the same `-u`.
    """
    cr = env.cr

    for label, sql in _ORPHAN_CLEANUPS:
        try:
            with cr.savepoint():
                cr.execute(sql)
                rows = cr.rowcount
            _logger.info("mint_mail_fk_hardening: cleaned %s -> %s rows", label, rows)
        except Exception as e:
            _logger.warning("mint_mail_fk_hardening: skipped cleanup %s (%s)", label, e)

    for table, fk, col, ref, ondelete, not_valid in _FK_FIXES:
        try:
            with cr.savepoint():
                cr.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {fk}")
                nv = " NOT VALID" if not_valid else ""
                cr.execute(
                    f"ALTER TABLE {table} "
                    f"ADD CONSTRAINT {fk} "
                    f"FOREIGN KEY ({col}) REFERENCES {ref}(id) ON DELETE {ondelete}{nv}"
                )
            _logger.info("mint_mail_fk_hardening: %s.%s -> %s ON DELETE %s%s",
                         table, col, ref, ondelete, " (NOT VALID)" if not_valid else "")
        except Exception as e:
            _logger.warning("mint_mail_fk_hardening: skipped FK %s on %s (%s)", fk, table, e)
