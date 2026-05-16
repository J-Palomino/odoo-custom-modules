{
    "name": "Mint: Mail FK Hardening",
    "summary": "Repair missing/non-CASCADE FKs on mail.* tables that break module installs",
    "description": """
This DB has accumulated missing or non-CASCADE Postgres FKs on several
mail-related tables. Odoo's ORM precheck blocks unlink operations when
it can't guarantee the FK behavior, breaking module installs that touch
mail.message / mail.followers garbage collection.

Applied fixes (idempotent, NOT VALID where the table is large):
  - mail_followers_mail_message_subtype_rel: both FK sides -> ON DELETE CASCADE
    + drop orphan rel rows pointing at deleted followers
  - mail_tracking_value.mail_message_id: add missing FK -> ON DELETE CASCADE
  - discuss_channel_member.fetched_message_id: add missing FK -> ON DELETE SET NULL
  - discuss_channel_member.seen_message_id:    add missing FK -> ON DELETE SET NULL
  - mail.notification: drop rows whose mail_mail_id targets a deleted mail.mail

Re-runs every install/upgrade so DB clones (staging refresh, etc.) self-heal.
""",
    "version": "19.0.1.0.0",
    "category": "Technical",
    "license": "LGPL-3",
    "author": "Mint Cannabis",
    "depends": ["mail"],
    "data": [],
    "installable": True,
    "application": False,
    "auto_install": False,
    "post_init_hook": "post_init_hook",
    "post_load": None,
}
