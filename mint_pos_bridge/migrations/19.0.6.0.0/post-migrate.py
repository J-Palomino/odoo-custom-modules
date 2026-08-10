# -*- coding: utf-8 -*-
"""Phase 6 partner merge (issue #278): supporting indexes + constraint sanity.

1. Expression indexes so the digits-normalized phone probe and the lowercased
   email probe used by _find_merge_candidates (and the resolver's phone tier)
   are index scans instead of 1.79M-row seq scans. Plain CREATE INDEX (not
   CONCURRENTLY - migrations run inside a transaction); expect a ~30-90s
   write lock on res_partner, run upgrades off-hours.

2. Sanity-check that exactly one UNIQUE constraint covers
   x_dutchie_customer_id (historically declared by two modules; ownership
   is consolidated on mint_dutchie_sync). Drops extras defensively.

3. Log stub/flag baselines so the first cron runs can be compared.

Idempotent.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute(
        r"""CREATE INDEX IF NOT EXISTS res_partner_phone_last10_idx
                ON res_partner (RIGHT(REGEXP_REPLACE(phone, '\D', '', 'g'), 10))
             WHERE phone IS NOT NULL"""
    )
    cr.execute(
        """CREATE INDEX IF NOT EXISTS res_partner_email_lower_idx
               ON res_partner (LOWER(email))
            WHERE email IS NOT NULL"""
    )

    cr.execute(
        """SELECT con.conname
             FROM pg_constraint con
             JOIN pg_class rel ON rel.oid = con.conrelid
             JOIN pg_attribute att
               ON att.attrelid = rel.oid AND att.attnum = ANY (con.conkey)
            WHERE rel.relname = 'res_partner'
              AND con.contype = 'u'
              AND att.attname = 'x_dutchie_customer_id'"""
    )
    names = sorted({r[0] for r in cr.fetchall()})
    if len(names) > 1:
        keep = ('res_partner_dutchie_customer_id_unique'
                if 'res_partner_dutchie_customer_id_unique' in names else names[0])
        for name in names:
            if name != keep:
                cr.execute('ALTER TABLE res_partner DROP CONSTRAINT "%s"' % name)
                _logger.warning(
                    'mint_pos_bridge: dropped duplicate UNIQUE constraint %s '
                    'on x_dutchie_customer_id (kept %s)', name, keep)
    else:
        _logger.info(
            'mint_pos_bridge: x_dutchie_customer_id UNIQUE constraints: %s',
            names or 'NONE')

    cr.execute(
        """SELECT COUNT(*) FILTER (WHERE x_is_stub),
                  COUNT(*) FILTER (WHERE x_merge_needs_review)
             FROM res_partner WHERE active"""
    )
    stubs, flagged = cr.fetchone()
    _logger.info(
        'mint_pos_bridge: partner-merge baseline: %s stubs, %s flagged for review',
        stubs, flagged)
