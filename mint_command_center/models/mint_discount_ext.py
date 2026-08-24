import json
import logging
import secrets
import urllib.request
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError, UserError

from .deal_mixins import weekday_bools_from_days

_logger = logging.getLogger(__name__)

# ── Welcome free pre-roll config (task #102149) ──────────────────────────────
# Per-LSP config for the welcome-coupon issuer. Dutchie category ids are PER-LSP
# (like brand ids), so the pre-roll category must be resolved per LSP. Seeded
# with AZ only (LSP 575 -> pre-roll category 34559, verified live vs PHXNTPR);
# other markets stay unissued until their id is added AND the LSP is enabled.
WELCOME_PREROLL_CATEGORY_PARAM = 'mint.welcome_preroll.lsp_category_json'  # {"<lsp>": <dutchie_category_id>}
WELCOME_PREROLL_ENABLED_LSPS_PARAM = 'mint.welcome_preroll.enabled_lsps'   # CSV of LSP ids to fan out to
WELCOME_PREROLL_LAUNCH_CUTOFF_PARAM = 'mint.welcome_preroll.launch_cutoff'  # only issue to partners created >= this (ISO); blank = disabled
WELCOME_PREROLL_TTL_DAYS_PARAM = 'mint.welcome_preroll.ttl_days'            # coupon validity window
DEFAULT_WELCOME_PREROLL_CATEGORY_JSON = '{"575": 34559}'

# ── Welcome coupon code format ───────────────────────────────────────────────
# Budtenders key these in by hand at the register, so the code has to be short
# and unambiguous when read aloud or off a phone screen.
#
# It must ALSO be unguessable. Dutchie applies no customer restriction to these
# discounts (RestrictToGroupIds/RestrictToSegmentIds are empty and there is no
# customer field), so the secrecy of the code is the ONLY thing stopping one
# customer from redeeming another's reward. The previous format was
# WELCOME-PR-<partner.id> — sequential, and issued partner ids land on
# consecutive integers in practice, so a customer could add 1 to their own code
# and take a stranger's pre-roll. Never derive this code from a record id.
WELCOME_CODE_PREFIX = 'MINT'
WELCOME_CODE_LEN = 6
# Crockford-style: no 0/O, 1/I/L (misread), no U (avoids accidental profanity).
WELCOME_CODE_ALPHABET = '23456789ABCDEFGHJKMNPQRSTVWXYZ'
# 30**6 = 729M combinations.
WELCOME_CODE_MAX_TRIES = 12
# Dates are stored as Date but issuance runs in UTC. Arizona is UTC-7, so a
# signup after 5pm local lands on the next UTC day and the coupon would be
# future-dated — Dutchie then rejects it for the rest of that evening, exactly
# when someone is most likely to walk into a store. Backdating by a day is
# cheaper and more robust than resolving each customer's store timezone, and
# covers every US market (worst case UTC-10).
WELCOME_VALID_FROM_SLACK_DAYS = 1


class MintDiscountPTL(models.Model):
    """Extend mint.discount with PTL-specific fields and lifecycle cron.

    The single Boolean `is_active` defined in mint_api_v2 is overridden here
    as a stored compute. Marketing flips `is_published`; the compute folds
    that with the valid window + day-of-week + start/end_time fields to
    yield `is_active` (which the REST API, POS, and Redis push consume).
    """
    _inherit = 'mint.discount'

    # Day-of-week availability (computed from PTL day associations)
    monday = fields.Boolean(string="Monday", default=False)
    tuesday = fields.Boolean(string="Tuesday", default=False)
    wednesday = fields.Boolean(string="Wednesday", default=False)
    thursday = fields.Boolean(string="Thursday", default=False)
    friday = fields.Boolean(string="Friday", default=False)
    saturday = fields.Boolean(string="Saturday", default=False)
    sunday = fields.Boolean(string="Sunday", default=False)

    # Source tracking
    source = fields.Selection([
        ('ptl', 'PTL'),
        ('dutchie', 'Dutchie POS'),
        ('manual', 'Manual'),
    ], string="Source", default='manual')

    # Link to PTL deal template
    ptl_deal_id = fields.Many2one(
        'mint.ptl.deal',
        string="PTL Deal",
        ondelete='set null',
    )

    # Welcome free pre-roll (task #102149): marks a per-customer single-use code
    # coupon issued on web sign-up. The Dutchie push branches on this to emit the
    # fixed pre-roll reward shape; the customer partner is on redemption_partner_id.
    is_welcome_preroll = fields.Boolean(
        string="Welcome Pre-Roll", default=False, copy=False, index=True,
        help="Auto-issued welcome free pre-roll code coupon (one per web signup).")

    welcome_push_sent = fields.Boolean(
        string="Welcome Push Sent", default=False, copy=False,
        help="The 'your free pre-roll is ready' push has been delivered for this "
             "coupon. Set only on a CONFIRMED send, so a failed delivery retries "
             "on the customer's next subscribe rather than being lost.")

    def _send_welcome_coupon_push(self, partner):
        """Push the welcome coupon to a partner who just enabled notifications.

        Onboarding step 3 asks for push, and the pay-off is this: the free
        pre-roll lands as a notification instead of the customer having to go
        look for it. Sent from the push subscription's create() (see
        push_subscription_ext) so it fires exactly when they opt in.

        Only for a coupon that is genuinely usable — an already redeemed or
        expired one must not be announced. welcome_push_sent is flipped only
        after a confirmed delivery, so a transient push failure retries on the
        next subscribe instead of silently dropping the notification.
        """
        if not partner:
            return 0
        coupon = self.sudo().search([
            ('is_welcome_preroll', '=', True),
            ('redemption_partner_id', '=', partner.id),
            ('welcome_push_sent', '=', False),
        ], order='id desc', limit=1)
        if not coupon:
            return 0
        # Mirror the /rewards reader's notion of "usable": not used, not expired.
        if coupon.redemption_status in ('used', 'expired', 'voided'):
            return 0
        # valid_until is a Date, so compare against a date. Against
        # fields.Datetime.now() this raises TypeError ("can't compare
        # datetime.datetime to datetime.date"), which the caller's best-effort
        # except swallows — silently dropping EVERY welcome push.
        if coupon.valid_until and coupon.valid_until < fields.Date.context_today(coupon):
            return 0

        sent = self.env['mint.push.subscription'].sudo().send_to_partner(
            partner.id,
            'Your free pre-roll is ready',
            'Show your code at any Mint AZ store — 100% off one pre-roll.',
            url='/rewards',
        )
        if sent:
            coupon.sudo().write({'welcome_push_sent': True})
            _logger.info('welcome_preroll: pushed coupon %s to partner %s (%s subs)',
                         coupon.dutchie_discount_code, partner.id, sent)
        return sent

    @api.constrains('application_method', 'dutchie_discount_code', 'source')
    def _check_code_method_has_code(self):
        """A code-method discount we author must carry the register code it is
        published with. Without it the Dutchie push emits ApplicationMethodId=3
        + an empty DiscountCode — an invalid coupon Dutchie rejects (or, worse,
        one redeemable with a blank code).

        Scoped to source != 'dutchie': Dutchie-mirrored discounts legitimately
        carry application_method='code' without our dutchie_discount_code field
        populated, and the read-sync must write them without tripping this
        guard. Only Odoo-authored discounts (PTL / welcome coupons) are held to
        the rule.
        """
        for rec in self:
            if (rec.source != 'dutchie'
                    and rec.application_method == 'code'
                    and not (rec.dutchie_discount_code or '').strip()):
                raise ValidationError(_(
                    "A discount with Application Method 'Code' requires a "
                    "Dutchie Discount Code (the code typed at the register)."
                ))

    @api.model
    def _welcome_preroll_config(self):
        """Resolved config for the welcome free pre-roll issuer (task #102149).

        Returns a dict:
          lsp_categories : {int lsp_id: int dutchie_category_id}
          enabled_lsps   : [int] LSPs to actually fan out to (subset of the map)
          launch_cutoff  : str ISO datetime or '' — only partners created on/after
                           this are issued (blank ⇒ issuer disabled, no backfill)
          ttl_days       : int coupon validity window (default 30)

        Values come from ir.config_parameter so ops can enable markets without a
        code change. No behavior on its own — consumed by Phase 2's issuer.
        """
        get = self.env['ir.config_parameter'].sudo().get_param
        try:
            raw = json.loads(get(WELCOME_PREROLL_CATEGORY_PARAM,
                                 DEFAULT_WELCOME_PREROLL_CATEGORY_JSON) or '{}')
            lsp_categories = {int(k): int(v) for k, v in raw.items()}
        except (ValueError, TypeError):
            _logger.warning('welcome_preroll: bad %s JSON; using none',
                            WELCOME_PREROLL_CATEGORY_PARAM)
            lsp_categories = {}
        enabled = []
        for tok in (get(WELCOME_PREROLL_ENABLED_LSPS_PARAM, '') or '').split(','):
            tok = tok.strip()
            if tok.isdigit() and int(tok) in lsp_categories:
                enabled.append(int(tok))
        try:
            ttl_days = int(get(WELCOME_PREROLL_TTL_DAYS_PARAM, '30') or 30)
        except (ValueError, TypeError):
            ttl_days = 30
        return {
            'lsp_categories': lsp_categories,
            'enabled_lsps': enabled,
            'launch_cutoff': (get(WELCOME_PREROLL_LAUNCH_CUTOFF_PARAM, '') or '').strip(),
            'ttl_days': ttl_days,
        }

    @api.model
    def _generate_welcome_code(self):
        """Return a fresh, unguessable, budtender-typeable coupon code.

        Uses secrets (not random) because this value is the only access control
        on the reward — see WELCOME_CODE_PREFIX above. Retries on the (unlikely)
        collision rather than trusting 729M combinations blindly, since a
        duplicate code would let two customers race for one Dutchie redemption.
        """
        Discount = self.sudo()
        for _attempt in range(WELCOME_CODE_MAX_TRIES):
            body = ''.join(secrets.choice(WELCOME_CODE_ALPHABET)
                           for _ in range(WELCOME_CODE_LEN))
            code = f'{WELCOME_CODE_PREFIX}-{body}'
            if not Discount.search_count([('dutchie_discount_code', '=', code)]):
                return code
        raise UserError(_('Could not generate a unique welcome coupon code.'))

    def _issue_welcome_preroll(self, partner):
        """Idempotently create + publish a welcome free pre-roll for one partner.

        Creates a single-use code coupon (ExternalId lgm_welcome_<pid>) and
        publishes it to Dutchie via the existing PTL canary (_push_one_discount),
        once per enabled LSP (discounts are LSP-scoped, so one write per LSP
        covers every store in it). Mode-gated by mint.dutchie_discount_push.mode:
        off ⇒ record created but no push; dry-run ⇒ logged; live ⇒ POSTed.
        Returns the coupon record (or None when the issuer is disabled).
        """
        cfg = self._welcome_preroll_config()
        if not cfg['enabled_lsps'] or not cfg['launch_cutoff']:
            return None  # issuer disabled until ops sets cutoff + enables an LSP

        coupon = self.sudo().search([
            ('is_welcome_preroll', '=', True),
            ('redemption_partner_id', '=', partner.id),
        ], limit=1)
        if not coupon:
            # valid_from/valid_until are Date fields; writing Datetime.now()
            # into them truncated the UTC instant, future-dating evening
            # signups. Compute in dates and back off by the slack day.
            starts = (fields.Date.context_today(self)
                      - timedelta(days=WELCOME_VALID_FROM_SLACK_DAYS))
            coupon = self.sudo().create({
                'name': (f'Welcome Free Pre-Roll — {partner.name or partner.id}')[:120],
                'source': 'ptl',
                'application_method': 'code',
                'dutchie_discount_code': self._generate_welcome_code(),
                'maximum_usage_count': 1,
                'is_welcome_preroll': True,
                'redemption_partner_id': partner.id,
                'is_available_online': False,
                'valid_from': starts,
                'valid_until': starts + timedelta(days=cfg['ttl_days']),
            })

        push = self.env['mint.ptl.day'].sudo()
        mode = push._get_dutchie_push_mode()
        if mode == 'off':
            return coupon
        url = push._get_dutchie_push_url()
        api_key = push._get_dutchie_push_api_key()
        Log = self.env['mint.dutchie.discount.push.log'].sudo()
        Company = self.env['res.company'].sudo()
        for lsp in cfg['enabled_lsps']:
            store = Company.search([
                ('dutchie_lsp_id', '=', lsp),
                ('dutchie_pos_location_id', '!=', False),
            ], limit=1)
            if not store:
                _logger.warning('welcome_preroll: no store with a POS LocId for LSP %s', lsp)
                continue
            try:
                push._push_one_discount(coupon, store, mode, url, api_key, Log)
            except Exception as e:
                _logger.warning('welcome_preroll: publish failed for partner %s LSP %s: %s',
                                partner.id, lsp, e)
        # Announce now if the partner already holds a push subscription. The
        # subscribe-time announce (push_subscription_ext) only helps when the
        # coupon exists BEFORE the opt-in — true for form signups (inline
        # issue), inverted for Google signups, whose coupon arrives from the
        # sweep cron up to 30 min after onboarding asked for push. Best-effort:
        # _send_welcome_coupon_push no-ops without a subscription and flips
        # welcome_push_sent only on confirmed delivery.
        if mode == 'live':
            try:
                self._send_welcome_coupon_push(partner)
            except Exception as e:
                _logger.warning('welcome_preroll: announce failed for partner %s: %s',
                                partner.id, e)
        return coupon

    @api.model
    def _cron_issue_welcome_prerolls(self):
        """Sweep recent web-signup partners lacking a welcome pre-roll and issue.

        Bounded batch; idempotent (skips partners who already have one). Off until
        ops sets mint.welcome_preroll.launch_cutoff + enabled_lsps.
        """
        cfg = self._welcome_preroll_config()
        if not cfg['enabled_lsps'] or not cfg['launch_cutoff']:
            return
        Partner = self.env['res.partner'].sudo()
        partners = Partner.search([
            ('is_web_customer', '=', True),
            ('create_date', '>=', cfg['launch_cutoff']),
        ], order='create_date asc', limit=200)
        if not partners:
            return
        have = set(self.sudo().search([
            ('is_welcome_preroll', '=', True),
            ('redemption_partner_id', 'in', partners.ids),
        ]).mapped('redemption_partner_id').ids)
        for p in partners:
            if p.id in have:
                continue
            try:
                self._issue_welcome_preroll(p)
            except Exception as e:
                _logger.warning('welcome_preroll: issue failed for partner %s: %s', p.id, e)

        # Retry announcements that had nothing to attach to at subscribe time:
        # a coupon issued after its partner opted into push (the Google-signup
        # order) sits with welcome_push_sent=False forever unless something
        # re-attempts. _send_welcome_coupon_push re-checks usability, no-ops
        # without a subscription, and marks sent only on confirmed delivery,
        # so this sweep is idempotent and bounded.
        unsent = self.sudo().search([
            ('is_welcome_preroll', '=', True),
            ('welcome_push_sent', '=', False),
            ('redemption_status', '=', 'pending'),
            ('redemption_partner_id', '!=', False),
        ], limit=200)
        for c in unsent:
            try:
                self._send_welcome_coupon_push(c.redemption_partner_id)
            except Exception as e:
                _logger.warning('welcome_preroll: announce retry failed for partner %s: %s',
                                c.redemption_partner_id.id, e)

    @api.model
    def _cron_sync_welcome_redemptions(self):
        """Flip welcome coupons to used/expired by reading Dutchie back.

        Expiry is derivable locally (valid_until). Redemption is read from Dutchie
        via mintinvsvc GET /api/admin/dutchie-discount/<id> (RedemptionCount) per
        live push-log row — the coupon is LSP-scoped, so we sum across LSPs. Marks
        redemption_status='used' once the total hits maximum_usage_count.
        """
        coupons = self.sudo().search([
            ('is_welcome_preroll', '=', True),
            ('redemption_status', 'not in', ['used', 'voided']),
        ], limit=500)
        if not coupons:
            return
        push = self.env['mint.ptl.day'].sudo()
        mode = push._get_dutchie_push_mode()
        api_key = push._get_dutchie_push_api_key()
        base = (push._get_dutchie_push_url() or '').rsplit('/api/admin/discounts', 1)[0]
        Log = self.env['mint.dutchie.discount.push.log'].sudo()
        # valid_until is a Date — compare date-to-date. Against a Datetime this
        # raises TypeError and aborts the whole sweep on its first coupon.
        today = fields.Date.context_today(self)
        for c in coupons:
            # Local expiry first — independent of Dutchie.
            if c.valid_until and c.valid_until < today:
                c.write({'redemption_status': 'expired'})
                continue
            if mode != 'live' or not base:
                continue  # only real redemptions to sync when we actually pushed live
            logs = Log.search([
                ('discount_id', '=', c.id), ('mode', '=', 'live'),
                ('success', '=', True), ('dutchie_discount_id', '!=', 0),
            ])
            total, seen = 0, False
            for lg in logs:
                store = lg.company_id
                loc = push._resolve_pos_loc_id(store)
                lsp = push._resolve_lsp_id(store)
                if not loc or not lsp:
                    continue
                u = (f'{base}/api/admin/dutchie-discount/{lg.dutchie_discount_id}'
                     f'?locId={loc}&lspId={lsp}')
                try:
                    req = urllib.request.Request(u, headers={
                        'X-API-Key': api_key,
                        'User-Agent': 'mint-odoo-welcome-sync/1.0',
                    })
                    with urllib.request.urlopen(req, timeout=30) as resp:
                        rec = (json.loads(resp.read().decode('utf-8', 'replace')) or {}).get('discount') or {}
                        total += int(rec.get('RedemptionCount') or 0)
                        seen = True
                except Exception as e:
                    _logger.warning('welcome_sync: read failed coupon %s did %s: %s',
                                    c.id, lg.dutchie_discount_id, e)
            if seen and total >= int(c.maximum_usage_count or 1):
                c.write({'redemption_status': 'used', 'redemption_used_at': now})

    # ─── Code-coupon usage sync (Backoffice report 1082) ─────────────────

    redemption_used_count = fields.Integer(
        string='Times Redeemed', default=0, copy=False, readonly=True,
        help="Redemptions counted from Dutchie Backoffice report 1082. "
             "Dutchie's own RedemptionCount is NOT usable: it still read 0 "
             "after a redemption confirmed at the register, and across 42 "
             "welcome coupons has never marked a single one used.",
    )

    # Report 1082's column names are not documented anywhere in this codebase.
    # They are confirmed on the first live run via the invsvc probe
    # (GET /api/admin/discount-usage?...&keys=true). Until then, match on a set
    # of candidates rather than one hardcoded key: an exact-key lookup against
    # the wrong name fails SILENTLY (0 rows matched reads identically to "not
    # redeemed"), which is the failure mode that made RedemptionCount look fine
    # for months. The observed keys are logged once per sweep so this list can
    # be tightened from real data instead of guessed at again.
    _USAGE_ID_KEYS = ('discountid', 'discount_id', 'id')
    _USAGE_ORDER_KEYS = ('orderid', 'order_id', 'ordernumber', 'order_number',
                         'transactionid', 'transaction_id', 'receiptno',
                         'receipt_no', 'register_transaction_id')

    @staticmethod
    def _usage_row_get(row, candidates):
        """Read a value from a report row by any of `candidates`, ignoring case,
        spaces and underscores. Dutchie report columns are human-facing labels
        ("Order Id", "discountId", "Discount ID"), so one exact key is a
        coin flip."""
        norm = {}
        for k, v in (row or {}).items():
            norm[str(k).lower().replace(' ', '').replace('_', '')] = v
        for cand in candidates:
            v = norm.get(cand.replace('_', ''))
            if v not in (None, ''):
                return v
        return None

    @api.model
    def _cron_sync_code_coupon_usage(self):
        """Count code-coupon redemptions from Dutchie report 1082.

        Replaces the RedemptionCount path, which does not work (see the field
        help above). The ingested POS data cannot answer this either: an
        order-level "$X off total" coupon discounts the ORDER, so it never
        appears in mint.pos.order.line.dutchie_discounts_json (verified: zero
        lines reference any of four known order-total coupons), and
        mint.pos.order stores only a bare `discount_total` amount with no id.
        Report 1082 is the only source that names WHICH discount a transaction
        used.

        Counts DISTINCT orders, not rows: an order-total discount can be
        reported once per affected line, so counting rows would burn a
        multi-use coupon several times over on a single basket.
        """
        coupons = self.sudo().search([
            ('application_method', '=', 'code'),
            ('is_welcome_preroll', '=', False),
            ('redemption_status', 'not in', ['used', 'voided']),
            ('dutchie_discount_code', '!=', False),
        ], limit=200)
        if not coupons:
            return 0

        push = self.env['mint.ptl.day'].sudo()
        if push._get_dutchie_push_mode() != 'live':
            return 0
        api_key = push._get_dutchie_push_api_key()
        base = (push._get_dutchie_push_url() or '').rsplit('/api/admin/discounts', 1)[0]
        if not base or not api_key:
            _logger.warning('coupon_usage_sync: invsvc url/key unset — skipped')
            return 0

        Log = self.env['mint.dutchie.discount.push.log'].sudo()
        today = fields.Date.context_today(self)
        logged_keys = False
        updated = 0

        for c in coupons:
            if c.valid_until and c.valid_until < today:
                c.write({'redemption_status': 'expired'})
                continue
            logs = Log.search([
                ('discount_id', '=', c.id), ('mode', '=', 'live'),
                ('success', '=', True), ('dutchie_discount_id', '!=', 0),
            ])
            if not logs:
                continue
            start = c.valid_from or (c.create_date and c.create_date.date()) or today
            frm = '%d/%d/%d' % (start.month, start.day, start.year)
            to = '%d/%d/%d' % (today.month, today.day, today.year)

            wanted_ids = {str(lg.dutchie_discount_id) for lg in logs}
            code = (c.dutchie_discount_code or '').strip().lower()
            orders = set()
            seen_any = False
            # One call per (loc, lsp) the coupon was actually pushed to.
            for loc, lsp in {(push._resolve_pos_loc_id(lg.company_id),
                              push._resolve_lsp_id(lg.company_id)) for lg in logs}:
                if not loc or not lsp:
                    continue
                url = ('%s/api/admin/discount-usage?locId=%s&lspId=%s&from=%s&to=%s'
                       % (base, loc, lsp, frm, to))
                try:
                    req = urllib.request.Request(url, headers={
                        'X-API-Key': api_key,
                        'User-Agent': 'mint-odoo-coupon-usage-sync/1.0',
                    })
                    with urllib.request.urlopen(req, timeout=120) as resp:
                        payload = json.loads(resp.read().decode('utf-8', 'replace')) or {}
                except Exception as e:
                    _logger.warning('coupon_usage_sync: report read failed '
                                    'coupon=%s loc=%s: %s', c.id, loc, e)
                    continue
                rows = payload.get('rows') or []
                seen_any = True
                if rows and not logged_keys:
                    _logger.info('coupon_usage_sync: report 1082 columns = %s',
                                 sorted(rows[0].keys()))
                    logged_keys = True
                for row in rows:
                    rid = self._usage_row_get(row, self._USAGE_ID_KEYS)
                    matched = rid is not None and str(rid) in wanted_ids
                    if not matched and code:
                        matched = code in json.dumps(row, default=str).lower()
                    if not matched:
                        continue
                    oid = self._usage_row_get(row, self._USAGE_ORDER_KEYS)
                    orders.add(str(oid) if oid is not None else 'row:%d' % id(row))

            if not seen_any:
                continue
            count = len(orders)
            vals = {}
            if count != c.redemption_used_count:
                vals['redemption_used_count'] = count
            cap = int(c.maximum_usage_count or 0)
            if cap and count >= cap:
                vals.update({
                    'redemption_status': 'used',
                    'redemption_used_at': fields.Datetime.now(),
                    'is_published': False,
                })
            if vals:
                c.write(vals)
                updated += 1
                _logger.info('coupon_usage_sync: %s used %s/%s', c.dutchie_discount_code,
                             count, cap or '-')
        return updated

    def action_publish_to_dutchie(self):
        """Manual 'Publish to Dutchie' button for a standalone (one-off) coupon.

        Emits ApplicationMethodId (from application_method), the DiscountCode, and
        MaxRedemptions (from maximum_usage_count), scoped by the Targeting
        restrictions, to each target store via the PTL canary's _push_one_discount.
        Gated by mint.dutchie_discount_push.mode (off/dry-run/live). Targets
        store_ids when set, else every push-enabled store with a POS LocId + LSP.
        """
        self.ensure_one()
        push = self.env['mint.ptl.day'].sudo()
        mode = push._get_dutchie_push_mode()
        if mode == 'off':
            raise UserError(_(
                "Dutchie push is OFF. Set mint.dutchie_discount_push.mode to "
                "'dry-run' or 'live' before publishing."))
        url = push._get_dutchie_push_url()
        api_key = push._get_dutchie_push_api_key()
        Log = self.env['mint.dutchie.discount.push.log'].sudo()
        Company = self.env['res.company'].sudo()
        stores = self.store_ids if self.store_ids else Company.search([
            ('dutchie_pos_location_id', '!=', False),
            ('dutchie_lsp_id', '!=', False),
            ('dutchie_discount_push_enabled', '=', True),
        ])
        if not stores:
            raise UserError(_(
                "No target stores. Pick stores under Targeting, or enable "
                "'Push Discounts to Dutchie' on a store that has a POS LocId + LSP."))
        ok = 0
        for store in stores:
            try:
                if push._push_one_discount(self, store, mode, url, api_key, Log):
                    ok += 1
            except Exception as e:
                _logger.warning('publish_to_dutchie: discount %s @ store %s failed: %s',
                                self.id, store.id, e)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Published to Dutchie'),
                'message': _('%(ok)s of %(n)s store(s) in %(mode)s mode — see the Dutchie push log.',
                             ok=ok, n=len(stores), mode=mode),
                'type': 'success' if ok else 'warning',
                'sticky': False,
            },
        }

    # ── Override is_active as stored compute ─────────────────────────────
    is_active = fields.Boolean(
        string="Active Now",
        store=True,
        index=True,
        compute='_compute_is_active',
        readonly=True,
        help="Computed: TRUE iff is_published AND today is in valid window "
             "AND today's dow flag is set AND now is within start/end_time "
             "(0/0 = all day).",
    )

    DAY_NAME_MAP = {
        0: 'monday', 1: 'tuesday', 2: 'wednesday', 3: 'thursday',
        4: 'friday', 5: 'saturday', 6: 'sunday',
    }

    @api.depends(
        'is_published', 'valid_from', 'valid_until',
        'monday', 'tuesday', 'wednesday', 'thursday',
        'friday', 'saturday', 'sunday',
        'start_time', 'end_time',
    )
    def _compute_is_active(self):
        now = fields.Datetime.context_timestamp(self, fields.Datetime.now())
        today = now.date()
        hour_now = now.hour + now.minute / 60.0
        dow_name = self.DAY_NAME_MAP[today.weekday()]
        for rec in self:
            in_window = bool(rec.is_published) \
                and (not rec.valid_from or rec.valid_from <= today) \
                and (not rec.valid_until or rec.valid_until >= today)
            dow_ok = bool(getattr(rec, dow_name, False))
            st = rec.start_time or 0.0
            et = rec.end_time or 0.0
            time_ok = (st == 0 and et == 0) or (st <= hour_now <= et)
            rec.is_active = in_window and dow_ok and time_ok

    # ── Daily lifecycle cron (was: flip is_active, now: flip is_published) ──
    @api.model
    def _cron_ptl_daily_lifecycle(self):
        """Daily lifecycle for PTL discounts: publish on valid_from, unpublish past valid_until."""
        today = fields.Date.today()

        # Publish PTL deals where valid_from <= today
        to_publish = self.search([
            ('source', '=', 'ptl'),
            ('is_published', '=', False),
            ('valid_from', '<=', today),
            '|', ('valid_until', '=', False), ('valid_until', '>=', today),
        ])
        if to_publish:
            to_publish.write({'is_published': True})
            _logger.info('PTL lifecycle: published %d discounts', len(to_publish))

        # Unpublish PTL deals where valid_until < today
        to_unpublish = self.search([
            ('source', '=', 'ptl'),
            ('is_published', '=', True),
            ('valid_until', '!=', False),
            ('valid_until', '<', today),
        ])
        if to_unpublish:
            to_unpublish.write({'is_published': False})
            _logger.info('PTL lifecycle: unpublished %d expired discounts', len(to_unpublish))

        # Recompute day-of-week booleans for active PTL discounts
        active_ptl = self.search([
            ('source', '=', 'ptl'),
            ('is_published', '=', True),
            ('ptl_deal_id', '!=', False),
        ])
        for discount in active_ptl:
            self._recompute_day_booleans(discount)

        # Push all changes to inventory service
        changed = to_publish | to_unpublish | active_ptl
        if changed:
            self.env['mint.ptl.day']._push_discounts_to_redis(changed.ids)

        _logger.info(
            'PTL lifecycle cron: %d published, %d unpublished, %d dow-recomputed',
            len(to_publish), len(to_unpublish), len(active_ptl),
        )

    # ── Hourly cron: push only edges (is_active flips) to Redis ──────────
    @api.model
    def _cron_recompute_active(self):
        """Hourly: recompute is_active edges, push only diffs to Redis.

        is_active is a stored compute, so we invalidate + read to force re-store.
        Then we diff against the captured 'before' set and push only deltas to
        keep webhook traffic low.
        """
        candidates = self.search([('is_published', '=', True)])
        if not candidates:
            return
        before = {r.id: r.is_active for r in candidates}
        candidates.invalidate_recordset(['is_active'])
        # Force the read to materialize the new compute values:
        _ = [r.is_active for r in candidates]
        flipped = [r.id for r in candidates if before.get(r.id) != r.is_active]
        if flipped:
            self.env['mint.ptl.day']._push_discounts_to_redis(flipped)
            _logger.info(
                'PTL hourly active recompute: %d records flipped, pushed to Redis',
                len(flipped),
            )
        else:
            _logger.debug('PTL hourly active recompute: no edges')

    def _compute_weekday_bools(self, market=None):
        """Monday..Sunday booleans for THIS discount from its deal's published
        PTL days, bounded by the discount's own validity span (NOT a fixed
        today+60 horizon, which made >60-day-out deals collapse to all-False =
        every-day) and optionally scoped to ``market`` for per-market Dutchie
        pushes. See deal_mixins.weekday_bools_from_days for the rule."""
        self.ensure_one()
        deal = self.ptl_deal_id
        if not deal:
            return {name: False for name in self.DAY_NAME_MAP.values()}
        days = self.env['mint.ptl.day'].search([
            ('deal_ids', 'in', deal.id),
            ('state', '=', 'published'),
        ])
        pairs = [(d.date, d.market_id.id) for d in days]
        return weekday_bools_from_days(
            pairs, self.valid_from, self.valid_until,
            market.id if market else None)

    def _recompute_day_booleans(self, discount):
        """Persist monday-sunday booleans from linked PTL day dates — the
        market-blind union, for the storefront/Redis view. The per-market
        Dutchie payload computes its OWN market-scoped flags so a deal scheduled
        in multiple states doesn't leak one market's weekdays into another
        (see _deal_to_dutchie_payload)."""
        if not discount.ptl_deal_id:
            return
        discount.write(discount._compute_weekday_bools())
