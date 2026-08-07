# -*- coding: utf-8 -*-
"""
res.partner extensions for Dutchie customer tracking.

Adds loyalty-id join key, provenance, and stub/review flags used by the
lane-watcher create-on-detect flow and the nightly merge cron.

Note: `x_dutchie_customer_id` (the primary strong-key) + its UNIQUE
constraint live on `mint_dutchie_sync` (this module depends on it —
see mint_pos_bridge/__manifest__.py). We do NOT redeclare the column
here to avoid a duplicate constraint on module upgrade.
"""
import logging

from psycopg2 import IntegrityError

from odoo import api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


PARTNER_ORIGINS = [
    ('odoo_manual', 'Odoo (manual)'),
    ('web_checkout', 'Web Checkout'),
    ('dutchie_weborder', 'Dutchie (Web Order)'),
    ('dutchie_walkin', 'Dutchie (Walk-in)'),
]


class ResPartner(models.Model):
    _inherit = 'res.partner'

    # Loyalty ID — secondary strong key when no customer_id is captured
    # (mint_dutchie_sync owns x_dutchie_customer_id).
    x_dutchie_loyalty_id = fields.Char(
        string='Dutchie Loyalty ID',
        index=True,
        copy=False,
    )

    # Provenance — which system created this partner record.
    x_partner_origin = fields.Selection(
        PARTNER_ORIGINS,
        string='Origin',
        default='odoo_manual',
        index=True,
    )

    # Stub = partner created with minimal info (typically anonymous walk-in
    # observed by the lane watcher before any identifying data arrives).
    # Cleared automatically once the partner has both a real name and at
    # least one of (phone, email, dutchie_customer_id).
    x_is_stub = fields.Boolean(
        string='Is Stub',
        default=False,
        index=True,
    )

    # Flagged by the nightly merge cron when a stub has ambiguous match
    # candidates (2+ strong-key matches, or only weak-key matches). These
    # require human review via POS Bridge → Customers → Needs Merge Review.
    x_merge_needs_review = fields.Boolean(
        string='Needs Merge Review',
        default=False,
        index=True,
    )

    # Audit trail.
    x_first_seen_at = fields.Datetime(string='First Seen')
    x_last_merged_at = fields.Datetime(string='Last Merged')

    # Badge for the Needs Merge Review list. Non-stored on purpose: only ever
    # computed for the handful of records on screen, never table-wide.
    x_merge_candidate_count = fields.Integer(
        string='Strong Candidates',
        compute='_compute_merge_candidate_count',
    )

    def _recompute_stub_flag(self):
        """Clear x_is_stub when the partner has enough identifying data."""
        for partner in self:
            if not partner.x_is_stub:
                continue
            has_identity = bool(
                partner.x_dutchie_customer_id
                or partner.x_dutchie_loyalty_id
                or partner.phone
                or partner.email
            )
            has_name = bool(partner.name and not partner.name.startswith('Walk-in #'))
            if has_identity and has_name:
                partner.x_is_stub = False

    # ------------------------------------------------------------------
    # Merge machinery (Phase 6, issue #278)
    # ------------------------------------------------------------------

    # Strong keys, in the priority order the resolver uses. Identity keys are
    # only auto-merge grade for dl:/mj: prefixes (government ids); nd:/ph:
    # identity keys are weak evidence (shared phones, name+dob collisions).
    _MERGE_STRONG_FIELDS = (
        'x_dutchie_customer_id',
        'x_dutchie_loyalty_id',
        'x_dutchie_identity_key',
    )
    _IDENTITY_STRONG_PREFIXES = ('dl:', 'mj:')

    def _is_pos_staff(self):
        """True when the partner is an employee or backs an internal user.

        Mirrors the resolver's staff guard: staff records must never gain or
        lose customer identity through merging.
        """
        self.ensure_one()
        return bool(self.employee or any(not u.share for u in self.user_ids))

    @api.model
    def _ids_by_phone_last10(self, phone10):
        """ids of active partners whose phone's last-10 digits equal phone10,
        regardless of stored formatting (raw-column ilike cannot match
        formatted numbers). Oldest first for a deterministic pick."""
        if not phone10:
            return []
        self.env.cr.execute(
            r"""SELECT id FROM res_partner
                 WHERE active AND phone IS NOT NULL
                   AND RIGHT(REGEXP_REPLACE(phone, '\D', '', 'g'), 10) = %s
                 ORDER BY id""",
            (phone10,))
        return [r[0] for r in self.env.cr.fetchall()]

    def _find_merge_candidates(self):
        """Return (strong, weak) candidate recordsets for this partner.

        Strong: exact match on another partner's dutchie customer id,
        loyalty id, or a dl:/mj: identity key. Weak: nd:/ph: identity keys,
        phone last-10, lowercased email. Never includes self.
        """
        self.ensure_one()
        Partner = self.env['res.partner'].sudo()
        strong = Partner.browse()
        weak = Partner.browse()

        if self.x_dutchie_customer_id:
            strong |= Partner.search([
                ('x_dutchie_customer_id', '=', self.x_dutchie_customer_id),
                ('id', '!=', self.id),
            ])
        if self.x_dutchie_loyalty_id:
            strong |= Partner.search([
                ('x_dutchie_loyalty_id', '=', self.x_dutchie_loyalty_id),
                ('id', '!=', self.id),
            ])
        identity_key = self.x_dutchie_identity_key or ''
        if identity_key:
            matches = Partner.search([
                ('x_dutchie_identity_key', '=', identity_key),
                ('id', '!=', self.id),
            ])
            if identity_key.startswith(self._IDENTITY_STRONG_PREFIXES):
                strong |= matches
            else:
                weak |= matches

        digits = ''.join(c for c in str(self.phone or '') if c.isdigit())[-10:]
        if len(digits) == 10:
            ids = [i for i in self._ids_by_phone_last10(digits) if i != self.id]
            if ids:
                weak |= Partner.browse(ids)
        email = (self.email or '').strip().lower()
        if email:
            weak |= Partner.search([
                ('email', '=ilike', email),
                ('id', '!=', self.id),
            ])

        weak -= strong
        return strong, weak

    def _compute_merge_candidate_count(self):
        for partner in self:
            try:
                strong, _weak = partner._find_merge_candidates()
                partner.x_merge_candidate_count = len(strong)
            except Exception:
                partner.x_merge_candidate_count = 0

    def _merge_into(self, primary, mode='auto', matched_key=None):
        """Merge self (source, deleted) into primary (survivor).

        Returns a (status, detail) tuple: ('merged', key), ('conflict', field),
        ('skipped', reason) or ('failed', reason). Never raises for the
        expected failure modes - callers flag for review instead.

        The stock wizard's _update_values copies the source's column values
        onto the destination WHILE THE SOURCE ROW STILL EXISTS, which trips
        UNIQUE(x_dutchie_customer_id) instantly. So: snapshot the strong-key
        union, blank the keys on the source, flush, merge, then write the
        union onto the survivor.
        """
        self.ensure_one()
        primary.ensure_one()
        if self.id == primary.id:
            return ('skipped', 'same partner')
        if self._is_pos_staff() or primary._is_pos_staff():
            return ('skipped', 'staff partner')

        for field_name in self._MERGE_STRONG_FIELDS:
            src_val, dst_val = self[field_name], primary[field_name]
            if src_val and dst_val and src_val != dst_val:
                (self | primary).write({'x_merge_needs_review': True})
                return ('conflict', field_name)

        union = {
            field_name: primary[field_name] or self[field_name] or False
            for field_name in self._MERGE_STRONG_FIELDS
        }
        first_seen = [d for d in (self.x_first_seen_at, primary.x_first_seen_at) if d]
        union['x_first_seen_at'] = min(first_seen) if first_seen else False
        if (primary.x_partner_origin or 'odoo_manual') == 'odoo_manual' \
                and self.x_partner_origin and self.x_partner_origin != 'odoo_manual':
            union['x_partner_origin'] = self.x_partner_origin

        src_desc = '%s (id=%s, origin=%s, customer_id=%s, identity=%s)' % (
            self.name, self.id, self.x_partner_origin or '-',
            self.x_dutchie_customer_id or '-', self.x_dutchie_identity_key or '-',
        )

        src_no_track = self.sudo().with_context(tracking_disable=True, mail_notrack=True)
        try:
            with self.env.cr.savepoint():
                src_no_track.write({f: False for f in self._MERGE_STRONG_FIELDS})
                self.env.flush_all()
                self.env['base.partner.merge.automatic.wizard'].sudo()._merge(
                    [self.id, primary.id], dst_partner=primary,
                )
                primary.sudo().with_context(tracking_disable=True).write(
                    dict(union, x_last_merged_at=fields.Datetime.now(),
                         x_merge_needs_review=False),
                )
        except (UserError, IntegrityError) as exc:
            # Savepoint rollback restored the source's keys. Parent/child
            # links, partners backing 2+ users, or a concurrent key claim all
            # land here - a human decides.
            reason = str(exc)[:200]
            partners = self.exists() | primary.exists()
            if partners:
                partners.write({'x_merge_needs_review': True})
            _logger.warning('Partner merge %s -> %s failed: %s',
                            self.id, primary.id, reason)
            return ('failed', reason)

        primary._recompute_stub_flag()
        self.env['mint.partner.merge.log'].sudo().create({
            'dst_partner_id': primary.id,
            'src_partner_desc': src_desc,
            'matched_key': matched_key or 'manual',
            'mode': mode,
        })
        _logger.info('Merged partner %s into %s (key=%s, mode=%s)',
                     src_desc, primary.id, matched_key, mode)
        return ('merged', matched_key)

    @api.model
    def _cron_dedupe_stubs(self, batch_limit=None, dry_run=None):
        """Nightly stub reconciliation (Phase 6 policy, decided in the plan):

        - exactly ONE non-stub strong-key candidate: auto-merge
        - 2+ strong candidates: flag x_merge_needs_review
        - weak-only candidates: flag, never merge
        - zero candidates: leave for the next night

        Ships with dry-run ENABLED (ir.config_parameter
        mint_pos_bridge.partner_merge_dry_run, default '1'): logs and
        mint.partner.merge.log rows only, no writes to partners. Flip to '0'
        after reviewing the dry-run output.
        """
        icp = self.env['ir.config_parameter'].sudo()
        if dry_run is None:
            dry_run = icp.get_param('mint_pos_bridge.partner_merge_dry_run', '1') != '0'
        batch = int(batch_limit or icp.get_param(
            'mint_pos_bridge.partner_merge_batch_limit', '500'))

        stubs = self.sudo().search(
            [('x_is_stub', '=', True), ('x_merge_needs_review', '=', False)],
            order='create_date asc', limit=batch,
        )
        merged = flagged = conflicts = skipped = 0
        Log = self.env['mint.partner.merge.log'].sudo()

        for stub in stubs:
            strong, weak = stub._find_merge_candidates()
            auto_targets = strong.filtered(lambda p: not p.x_is_stub)
            if len(auto_targets) == 1:
                if dry_run:
                    Log.create({
                        'dst_partner_id': auto_targets.id,
                        'src_partner_desc': '%s (id=%s)' % (stub.name, stub.id),
                        'matched_key': 'manual',
                        'mode': 'dry_run',
                        'notes': 'would auto-merge',
                    })
                    merged += 1
                    continue
                status, detail = stub._merge_into(auto_targets, mode='auto')
                if status == 'merged':
                    merged += 1
                    if merged % 50 == 0 and not self.env.registry.in_test_mode():
                        # Bound lock time on long batches; each merge is
                        # individually savepointed so a later failure cannot
                        # poison committed work.
                        self.env.cr.commit()
                elif status == 'conflict':
                    conflicts += 1
                else:
                    flagged += 1
            elif len(strong) >= 2 or (not strong and weak):
                if not dry_run:
                    stub.write({'x_merge_needs_review': True})
                flagged += 1
            else:
                skipped += 1

        # Pass B: flag (never merge) non-stub duplicate clusters on the
        # shareable strong keys. customer_id cannot duplicate (UNIQUE).
        group_cap = 200
        for field_name in ('x_dutchie_loyalty_id', 'x_dutchie_identity_key'):
            self.env.cr.execute(
                """SELECT %s FROM res_partner
                    WHERE active AND %s IS NOT NULL
                      AND COALESCE(x_merge_needs_review, false) = false
                    GROUP BY %s HAVING count(*) > 1
                    LIMIT %%s""" % (field_name, field_name, field_name),
                (group_cap,))
            dup_keys = [r[0] for r in self.env.cr.fetchall()]
            if dup_keys and not dry_run:
                dups = self.sudo().search([(field_name, 'in', dup_keys)])
                dups.write({'x_merge_needs_review': True})
            flagged += len(dup_keys)

        _logger.info(
            '[partner-merge] %s: %d merged, %d flagged, %d conflicts, '
            '%d skipped (batch=%d)',
            'DRY RUN' if dry_run else 'live', merged, flagged, conflicts,
            skipped, batch,
        )
        return {'merged': merged, 'flagged': flagged,
                'conflicts': conflicts, 'skipped': skipped, 'dry_run': dry_run}

    def action_find_merge_candidates(self):
        """Open the stock merge wizard preloaded with this partner and its
        strong candidates (manual review path)."""
        self.ensure_one()
        strong, weak = self._find_merge_candidates()
        candidates = (strong | weak.filtered(lambda p: not p._is_pos_staff()))[:2]
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'base.partner.merge.automatic.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_partner_ids': [(6, 0, (self | candidates).ids)],
                'default_dst_partner_id': self.id,
            },
        }

    def action_mark_merge_reviewed(self):
        self.write({'x_merge_needs_review': False})
