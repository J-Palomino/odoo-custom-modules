# -*- coding: utf-8 -*-
"""Backfill res_partner.is_customer_contact in SQL, before the ORM sees it.

``is_customer_contact`` is a *stored* computed field on a table with ~2.7M
rows. Left to itself, the upgrade would load every partner and run the Python
compute over all of them. This fills the column first, so the ORM finds it
already populated and has nothing to recompute.

Two details keep it cheap:

* The column is added with ``DEFAULT true``. On PostgreSQL 11+ that is a
  metadata-only change, no table rewrite — and ~99% of partners (2.67M of
  2.70M) really are customers, so ``true`` is the right bulk answer.
* Only the ~25k exceptions are then flipped to ``false``, which is a small,
  fast UPDATE instead of one touching every row.

The predicate is the SQL twin of ResPartner._mint_is_customer: a customer
marker, and not staff. Keep the two in step.
"""
import logging

_logger = logging.getLogger(__name__)

CUSTOMER_MARKER_ORIGINS = ('dutchie_walkin', 'dutchie_weborder', 'web_checkout')


def migrate(cr, version):
    if not version:
        return

    cr.execute("""
        ALTER TABLE res_partner
        ADD COLUMN IF NOT EXISTS is_customer_contact boolean DEFAULT true
    """)

    # Everything that is NOT (marker AND not staff) is not a customer.
    cr.execute("""
        UPDATE res_partner p
           SET is_customer_contact = false
         WHERE NOT (
                   (
                       COALESCE(p.is_web_customer, false)
                       OR p.x_partner_origin IN %s
                       OR COALESCE(p.x_dutchie_customer_id, '') <> ''
                   )
                   AND NOT (
                       COALESCE(p.employee, false)
                       OR EXISTS (
                           SELECT 1 FROM res_users u
                            WHERE u.partner_id = p.id
                              AND u.share = false
                       )
                   )
               )
           AND p.is_customer_contact IS DISTINCT FROM false
    """, (CUSTOMER_MARKER_ORIGINS,))
    _logger.info(
        "is_customer_contact backfill: flagged %s partner(s) as non-customers",
        cr.rowcount)

    # Any row still NULL (added before the DEFAULT, e.g. a re-run) is a
    # customer by the same rule.
    cr.execute("""
        UPDATE res_partner SET is_customer_contact = true
         WHERE is_customer_contact IS NULL
    """)
