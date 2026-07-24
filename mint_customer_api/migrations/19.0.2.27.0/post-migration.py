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
     that BOTH the email and the phone(last-10) resolve to — the same strict
     rule the controller uses — and repoint the survivor's login to it,
     marking it is_web_customer. A single-identifier-only match is NOT linked;
     it is logged for manual review.
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
    """Return the linkable Dutchie customer partner, or an empty/2-record set.

    Mirrors mint_customer_api.controllers.auth._find_linkable_dutchie_partner
    and applies the SAME strict rule: BOTH email and phone are required, each
    must resolve to exactly one unclaimed Dutchie customer, and both must point
    at the same record. Anything weaker returns a set the caller treats as
    "no match" (0) or "ambiguous" (2) so it skips and logs instead of guessing.

    Kept strict deliberately: this repoints a real login onto a roster record,
    exactly as the controller does, so it must not link anything the controller
    itself would refuse. Single-identifier orphans are left for manual review.
    """
    Partner = env['res.partner'].sudo()
    digits = ''.join(c for c in (phone or '') if c.isdigit())
    if not email or len(digits) < 10:
        return Partner.browse()

    base = [('x_dutchie_customer_id', '!=', False), ('user_ids', '=', False)]
    by_phone = Partner.search(
        base + [('phone', 'ilike', digits[-10:])], order='id asc', limit=2)
    by_email = Partner.search(
        base + [('email', '=ilike', email)], order='id asc', limit=2)

    # Ambiguous on either identifier -> hand back 2 records so the caller
    # records it as ambiguous (needs manual review) rather than linking.
    if len(by_phone) > 1 or len(by_email) > 1:
        return by_phone if len(by_phone) > 1 else by_email
    # Missing on either side, or the two disagree -> no confident match.
    if len(by_phone) != 1 or len(by_email) != 1 or by_phone.id != by_email.id:
        return Partner.browse()
    return by_phone


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
