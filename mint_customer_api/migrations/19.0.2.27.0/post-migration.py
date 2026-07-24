# -*- coding: utf-8 -*-
"""
Post-migration for mint_customer_api 19.0.2.27.0 — heal orphaned web customers.

Background:
  Web signup used to always create a fresh res.partner, so a customer who
  already had a Dutchie-backfilled partner (holding their loyalty + purchase
  history) got orphaned into a new partner with no x_dutchie_customer_id — so
  /rewards showed 0 points. Concurrent double-submits also created *two*
  res.users sharing the same `web:<email>` login (the website module replaced
  UNIQUE(login) with UNIQUE(login, website_id); NULLs don't collide). See
  MR-1089. The controller now adopts the existing Dutchie partner for NEW
  signups; this migration repairs the accounts already orphaned.

What it does, per active `web:<email>` login group:
  1. Keep the Dutchie-linked user as the survivor (lowest id only as the
     tie-break); free + archive any duplicates.
  2. If the survivor's partner has no x_dutchie_customer_id, look for the one
     unambiguous Dutchie customer partner (x_dutchie_customer_id set, no login)
     matching by phone(last-10) then email — mirroring the controller — and
     repoint the survivor's login to it, marking it is_web_customer.
  3. Archive the leftover orphan web partners (is_web_customer, no Dutchie id,
     no remaining active user).

Safety / mechanics:
  - Mutations use raw SQL (repoint / is_web_customer / archive) to bypass the
    ORM write hooks entirely; matching uses the ORM (reads).
  - Conservative: skips any login group whose email/phone maps to zero or more
    than one linkable Dutchie partner (logged for manual review) — never guesses.
  - Idempotent: once a survivor is repointed its partner has a Dutchie id (so it
    is skipped), duplicates/orphans are archived (so they fall out of the active
    filter). Re-running is a no-op.
  - Each login group is wrapped in a savepoint; one failure is logged and rolled
    back without aborting the rest of the migration.
"""
import logging

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)

WEB_LOGIN_PREFIX = 'web:'


def _linkable_dutchie_partners(env, email, phone):
    """Return candidate linkable Dutchie customer partners (capped at 2).

    Mirrors mint_customer_api.controllers.auth._find_linkable_dutchie_partner
    (Dutchie customer, no login, phone-then-email) but returns the recordset so
    the caller can tell "no match" (0) from "ambiguous" (>1) and never guess.
    """
    Partner = env['res.partner'].sudo()
    base = [('x_dutchie_customer_id', '!=', False), ('user_ids', '=', False)]
    digits = ''.join(c for c in (phone or '') if c.isdigit())

    match = Partner.browse()
    if len(digits) >= 10:
        match = Partner.search(
            base + [('phone', 'ilike', digits[-10:])], order='id asc', limit=2)
    if not match and email:
        match = Partner.search(
            base + [('email', '=ilike', email)], order='id asc', limit=2)
    return match


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    Partner = env['res.partner'].sudo()
    Users = env['res.users'].sudo()

    if 'x_dutchie_customer_id' not in Partner._fields:
        _logger.warning(
            'mint_customer_api heal: x_dutchie_customer_id field absent '
            '(mint_api_v2 not installed?) — skipping orphan heal.')
        return

    web_users = Users.search(
        [('login', '=like', WEB_LOGIN_PREFIX + '%'), ('active', '=', True)],
        order='id asc')

    # Group active web users by login.
    groups = {}
    for u in web_users:
        groups.setdefault(u.login, env['res.users'])
        groups[u.login] |= u

    claimed = set()          # dutchie partner ids already repointed this run
    stats = {'linked': 0, 'dups_archived': 0, 'orphans_archived': 0,
             'skipped_no_match': 0, 'skipped_ambiguous': 0, 'errors': 0}
    ambiguous_logins = []

    for login, users in groups.items():
        users = users.sorted('id')
        # Survivor = the account whose partner ALREADY carries the Dutchie
        # link; lowest id only as the tie-break.
        #
        # Picking the lowest id outright loses real data: the duplicate rows
        # are same-second double-submits, so which one the roster sync later
        # linked is arbitrary. Observed in prod — web:dariangcook@gmail.com had
        # the Dutchie id (16688117) on the HIGHER-id row (u538/p582988), so a
        # lowest-id rule would have archived the linked partner and discarded
        # that customer's identity. Keep the linked one and archive the empty
        # shell instead.
        linked = users.filtered(
            lambda u: u.partner_id and u.partner_id.x_dutchie_customer_id)
        survivor = linked[0] if linked else users[0]
        dups = users - survivor
        email = login[len(WEB_LOGIN_PREFIX):] if login.startswith(WEB_LOGIN_PREFIX) else login

        cr.execute('SAVEPOINT heal_login')
        try:
            survivor_partner = survivor.partner_id

            # --- 1. Link the survivor to its real Dutchie partner if orphaned.
            already_linked = bool(survivor_partner.x_dutchie_customer_id)
            if not already_linked:
                candidates = _linkable_dutchie_partners(env, email, survivor_partner.phone)
                candidates = candidates.filtered(lambda p: p.id not in claimed)
                if len(candidates) == 1:
                    dutchie = candidates
                    claimed.add(dutchie.id)
                    cr.execute(
                        "UPDATE res_users SET partner_id = %s, write_date = now() "
                        "WHERE id = %s", (dutchie.id, survivor.id))
                    cr.execute(
                        "UPDATE res_partner SET is_web_customer = true, "
                        "write_date = now() WHERE id = %s", (dutchie.id,))
                    # Archive the now-userless orphan web partner.
                    if (survivor_partner.id != dutchie.id
                            and not survivor_partner.x_dutchie_customer_id):
                        cr.execute(
                            "UPDATE res_partner SET active = false, write_date = now() "
                            "WHERE id = %s", (survivor_partner.id,))
                        stats['orphans_archived'] += 1
                    stats['linked'] += 1
                    _logger.info('heal: login %s -> Dutchie partner %s (%s)',
                                 login, dutchie.id, dutchie.x_dutchie_customer_id)
                elif len(candidates) > 1:
                    stats['skipped_ambiguous'] += 1
                    ambiguous_logins.append(login)
                    _logger.warning('heal: login %s — %s Dutchie matches, skipped '
                                    '(needs manual review)', login, len(candidates))
                else:
                    stats['skipped_no_match'] += 1
                    _logger.info('heal: login %s — no Dutchie match, left as-is', login)

            # --- 2. Free + archive duplicate users sharing this login.
            for dup in dups:
                dup_partner = dup.partner_id
                # Rename the login so it stops colliding, then archive the user.
                cr.execute(
                    "UPDATE res_users SET login = login || '.dup' || id || '.archived', "
                    "active = false, write_date = now() WHERE id = %s", (dup.id,))
                stats['dups_archived'] += 1
                # Archive the duplicate's orphan partner if it's a web orphan
                # with no Dutchie link and no other active user.
                # NB: read survivor_partner.id captured before the SQL repoint —
                # survivor.partner_id is stale-cached at this point.
                if (dup_partner and dup_partner.id != survivor_partner.id
                        and not dup_partner.x_dutchie_customer_id
                        and dup_partner.is_web_customer):
                    other = Users.search_count([
                        ('partner_id', '=', dup_partner.id),
                        ('active', '=', True), ('id', '!=', dup.id)])
                    if not other:
                        cr.execute(
                            "UPDATE res_partner SET active = false, write_date = now() "
                            "WHERE id = %s", (dup_partner.id,))
                        stats['orphans_archived'] += 1
                _logger.info('heal: archived duplicate user %s (login %s)', dup.id, login)

            cr.execute('RELEASE SAVEPOINT heal_login')
        except Exception:  # noqa: BLE001 — never let one login abort the batch
            cr.execute('ROLLBACK TO SAVEPOINT heal_login')
            stats['errors'] += 1
            _logger.exception('heal: failed for login %s — rolled back this group', login)

    env['res.users'].invalidate_model()
    env['res.partner'].invalidate_model()
    _logger.warning(
        'mint_customer_api orphan heal complete: linked=%(linked)s '
        'dups_archived=%(dups_archived)s orphans_archived=%(orphans_archived)s '
        'skipped_no_match=%(skipped_no_match)s skipped_ambiguous=%(skipped_ambiguous)s '
        'errors=%(errors)s', stats)
    if ambiguous_logins:
        _logger.warning('heal: %s ambiguous logins need manual review: %s',
                        len(ambiguous_logins), ', '.join(sorted(ambiguous_logins)))
