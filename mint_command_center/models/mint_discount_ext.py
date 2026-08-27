import json
import logging
import secrets
import urllib.parse
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

# How many PTL discounts _cron_ptl_daily_lifecycle may change state for in one
# run. The push it triggers costs one webhook payload per (discount × store),
# so an unbounded run against a backlog is quadratic in practice: 329 expired
# records across ~40 stores took prod Odoo down on 2026-08-25 by holding
# WorkerCron past limit_time_real (300s) until it was killed and retried.
# 100 keeps a run at roughly 4,000 payload builds — comfortably inside the
# limit — and a backlog simply drains across consecutive runs.
PTL_LIFECYCLE_BATCH_PARAM = 'mint_cc.ptl_lifecycle_batch'
PTL_LIFECYCLE_DEFAULT_BATCH = 100


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
            # order='id' is load-bearing: an unordered limit=1 let the chosen
            # store drift between runs, and because the push-log id lookup was
            # keyed on the store, a drifted pick CREATED a second LSP-wide
            # Dutchie record instead of updating the first (coupon 3046 ->
            # 385159 @ 75th Ave, then 385236 @ Tempe, same code MINT-58N83Z).
            store = Company.search([
                ('dutchie_lsp_id', '=', lsp),
                ('dutchie_pos_location_id', '!=', False),
            ], order='id', limit=1)
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
        """Deprecated — delegates to _cron_sync_code_coupon_usage().

        This used to read Dutchie's RedemptionCount per push-log row. That
        field never increments: it still read 0 after a redemption confirmed at
        the register, and across 42 welcome coupons in production this cron had
        never marked a single one used since it shipped in 2026-07. It looked
        healthy the whole time because "0 redemptions" is indistinguishable
        from "nothing to do".

        A welcome pre-roll is just a code coupon, so it is now swept by the
        same report-10875 path as every other one rather than maintaining a
        second, broken implementation. Kept as a wrapper so the existing
        ir.cron record keeps working; that record can be disabled, since the
        code-coupon cron already covers these.
        """
        return self._cron_sync_code_coupon_usage()

    # ─── Code-coupon usage sync (Backoffice report 10875) ────────────────

    promo_gift_token = fields.Char(
        string='Promo Gift Token', index=True, copy=False,
        help="Opaque, unguessable handle used in the /gift/<token> URL. The "
             "redeemable code must NEVER appear in a shareable link: a scanned "
             "QR puts the URL in the address bar, so a code-in-URL link is "
             "readable by anyone who sees the code — which defeats the "
             "sign-in gate entirely. This token is worthless at a register.",
    )

    promo_issued_by_id = fields.Many2one(
        'res.partner', string='Promo Issued By', index=True, copy=False,
        help="Set when this coupon was minted from the storefront Promos "
             "screen. Distinguishes an ad-hoc promo from a PTL deal or a "
             "loyalty redemption, and scopes the issuer's own promo list.",
    )

    redemption_used_count = fields.Integer(
        string='Times Redeemed', default=0, copy=False, readonly=True,
        help="Redemptions counted from Dutchie Backoffice report 10875 "
             "(Discount Detail), matched on Discount Code. "
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
    # Backoffice report 10875 "Discount Detail Report". Confirmed live
    # 2026-08-24 — columns: Location Name, Order ID, Order Time, Customer Name,
    # Product Name, Gross Sales, Discounted Amount, Discount Name, Discount
    # Description, Discount Code, Budtender Name (+ consumer groups).
    #
    # NOT report 1082 (Location/Order/Customer/Product/Gross/Discounted
    # Amount/Net): it carries a discount AMOUNT and never names the discount,
    # so it cannot attribute a redemption. NOT report 175 either — that one
    # names the discount but its QuantityUsed counts discounted LINE ITEMS,
    # reporting 2 for a single basket, which would burn two uses per visit.
    USAGE_REPORT_ID = 10875
    # Server-side row filter. Every code we mint is MINT-XXXXXX, so this
    # trims an ~8k-row location pull to a handful before it crosses the wire.
    USAGE_CODE_PREFIX = 'MINT-'
    _USAGE_CODE_KEYS = ('discountcode',)
    _USAGE_ORDER_KEYS = ('orderid',)

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
        """Count code-coupon redemptions from Backoffice report 10875.

        Report 10875 ("Discount Detail Report") is the only source that names
        WHICH discount a transaction used. Checked and rejected:
          * Dutchie RedemptionCount — never increments (still 0 after a
            redemption confirmed at the register).
          * mint.pos.order.line.dutchie_discounts_json — line-level only;
            order-total coupons never appear.
          * report 1082 — carries a `Discounted Amount` number, no identity.
          * report 175 — names the discount but QuantityUsed counts discounted
            LINE ITEMS (2 for a single basket), so it over-counts uses.

        Recomputed from valid_from each sweep rather than accumulated, so it is
        idempotent and self-healing: a missed run backfills on the next one.
        """
        coupons = self.sudo().search([
            ('application_method', '=', 'code'),
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

        today = fields.Date.context_today(self)
        live = coupons.filtered(lambda c: not (c.valid_until and c.valid_until < today))
        expired = coupons - live
        if expired:
            expired.write({'redemption_status': 'expired'})
        if not live:
            return len(expired)

        # Every store the coupons are scoped to — NOT just the one they were
        # pushed to. A code is LSP-wide (redeemable at any store in the LSP)
        # while report 10875 is LOCATION-scoped. Verified live: MINT-GMXT6F was
        # pushed to 75th Ave (loc 2679) and redeemed at Tempe (loc 1568);
        # querying 2679 returns 0 rows. Deriving locations from the push log
        # would report "never redeemed" forever, silently.
        targets = set()
        for c in live:
            for store in c.store_ids:
                loc = push._resolve_pos_loc_id(store)
                lsp = push._resolve_lsp_id(store)
                if loc and lsp:
                    targets.add((loc, lsp))
        if not targets:
            _logger.warning('coupon_usage_sync: no store scope on %d coupon(s)', len(live))
            return len(expired)

        starts = [c.valid_from or (c.create_date and c.create_date.date()) or today
                  for c in live]
        start = min(starts)
        frm = '%d/%d/%d' % (start.month, start.day, start.year)
        to = '%d/%d/%d' % (today.month, today.day, today.year)

        # One report fetch per location, reused across every coupon, rather
        # than per (coupon, location). `needle` trims the payload server-side —
        # a 3-day Tempe pull is ~8k rows unfiltered.
        by_code = {}
        fetched = 0
        for loc, lsp in sorted(targets):
            url = ('%s/api/admin/discount-usage?locId=%s&lspId=%s&from=%s&to=%s'
                   '&reportId=%s&needle=%s'
                   % (base, loc, lsp, frm, to, self.USAGE_REPORT_ID,
                      urllib.parse.quote(self.USAGE_CODE_PREFIX)))
            try:
                req = urllib.request.Request(url, headers={
                    'X-API-Key': api_key,
                    'User-Agent': 'mint-odoo-coupon-usage-sync/1.0',
                })
                with urllib.request.urlopen(req, timeout=240) as resp:
                    payload = json.loads(resp.read().decode('utf-8', 'replace')) or {}
            except Exception as e:
                _logger.warning('coupon_usage_sync: report read failed loc=%s: %s', loc, e)
                continue
            fetched += 1
            for row in payload.get('rows') or []:
                rcode = self._usage_row_get(row, self._USAGE_CODE_KEYS)
                oid = self._usage_row_get(row, self._USAGE_ORDER_KEYS)
                if not rcode or oid is None:
                    continue
                # DISTINCT orders: one redemption discounts every eligible line
                # and the report emits a row per line — MINT-GMXT6F produced 2
                # rows on order 178720095 for a single redemption.
                by_code.setdefault(str(rcode).strip().lower(), set()).add(str(oid))

        if not fetched:
            _logger.warning('coupon_usage_sync: every location fetch failed — '
                            'leaving counts untouched')
            return len(expired)

        updated = 0
        for c in live:
            code = (c.dutchie_discount_code or '').strip().lower()
            count = len(by_code.get(code, ()))
            vals = {}
            if count != c.redemption_used_count:
                vals['redemption_used_count'] = count
            cap = int(c.maximum_usage_count or 0)
            # A welcome pre-roll is single-use by construction. Several were
            # created by a raw RPC path that omitted maximum_usage_count and so
            # carry 0, which would read as "uncapped" and never flip to used.
            if not cap and c.is_welcome_preroll:
                cap = 1
            if cap and count >= cap:
                vals.update({
                    'redemption_status': 'used',
                    'redemption_used_at': fields.Datetime.now(),
                    'is_published': False,
                })
            if vals:
                c.write(vals)
                updated += 1
                _logger.info('coupon_usage_sync: %s used %s/%s',
                             c.dutchie_discount_code, count, cap or '-')
        return updated + len(expired)

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
        ], order='id')
        if not stores:
            raise UserError(_(
                "No target stores. Pick stores under Targeting, or enable "
                "'Push Discounts to Dutchie' on a store that has a POS LocId + LSP."))
        # A Dutchie discount is owned by the LSP, not the location, so one write
        # per LSP covers every store under it and repeated writes duplicate it.
        #
        # REGION-level targeting is therefore already correct: region <-> LSP is
        # 1:1 in production (AZ=575, MI=576, MO=723, IL=805, NV=820, FL=821 —
        # every region holds exactly one LSP and no LSP spans two regions), so
        # an LSP-scoped discount IS a region-scoped discount. Marketing's
        # region-specific deals need nothing further.
        #
        # What store_ids CANNOT do is narrow a deal to a subset of stores INSIDE
        # one region: the payload hardcodes LocationRestrictions=[], so a deal
        # targeted at one AZ store still goes live at all nine. If per-store
        # targeting is ever needed, populate LocationRestrictions from store_ids
        # — until then store_ids only selects which LSPs get written.
        stores = push._collapse_stores_by_lsp(stores)
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
    def _cron_ptl_daily_lifecycle(self, batch=None):
        """Daily lifecycle for PTL discounts: publish on valid_from, unpublish past valid_until.

        Bounded on purpose. When this cron was restored on 2026-08-25 after
        being switched off since June, its first unbounded run tried to clear a
        329-record backlog in one transaction and took prod down with it: the
        push builds one webhook payload per (discount × store), so ~600
        discounts across ~40 stores meant ~24,000 payload builds. WorkerCron
        blew through limit_time_real (300s), got killed, retried, and the HTTP
        workers starved alongside it — 61 `timeout after 300s` kills where the
        preceding hour had none.

        Two bounds keep that from recurring:

        1. `batch` caps how many records change state per run (default 100,
           overridable via the `mint_cc.ptl_lifecycle_batch` config parameter).
           A backlog drains over consecutive runs instead of one huge one.
        2. Only records that ACTUALLY changed are pushed. The old code pushed
           `to_publish | to_unpublish | active_ptl` — the entire published set,
           every single day, whether or not anything about it had moved. That
           made even a quiet day cost ~7,000 payload builds. Day-of-week
           booleans are still recomputed for the whole active set (cheap,
           in-memory), but only the ones whose flags moved get pushed.
        """
        today = fields.Date.today()
        Param = self.env['ir.config_parameter'].sudo()
        if batch is None:
            batch = int(Param.get_param(
                PTL_LIFECYCLE_BATCH_PARAM, PTL_LIFECYCLE_DEFAULT_BATCH))
        batch = max(1, batch)

        # Publish PTL deals where valid_from <= today
        publish_domain = [
            ('source', '=', 'ptl'),
            ('is_published', '=', False),
            ('valid_from', '<=', today),
            '|', ('valid_until', '=', False), ('valid_until', '>=', today),
        ]
        to_publish = self.search(publish_domain, order='id', limit=batch)
        if to_publish:
            to_publish.write({'is_published': True})
            _logger.info('PTL lifecycle: published %d discounts', len(to_publish))

        # Unpublish PTL deals where valid_until < today. Share the budget with
        # the publish pass so one run can never exceed `batch` state changes.
        unpublish_domain = [
            ('source', '=', 'ptl'),
            ('is_published', '=', True),
            ('valid_until', '!=', False),
            ('valid_until', '<', today),
        ]
        remaining_budget = batch - len(to_publish)
        to_unpublish = self.browse()
        if remaining_budget > 0:
            to_unpublish = self.search(
                unpublish_domain, order='id', limit=remaining_budget)
            if to_unpublish:
                to_unpublish.write({'is_published': False})
                _logger.info(
                    'PTL lifecycle: unpublished %d expired discounts', len(to_unpublish))

        # Recompute day-of-week booleans for active PTL discounts, tracking
        # which ones actually moved so the push stays proportional to real
        # change rather than to the size of the catalogue.
        active_ptl = self.search([
            ('source', '=', 'ptl'),
            ('is_published', '=', True),
            ('ptl_deal_id', '!=', False),
        ])
        dow_changed = self.browse()
        for discount in active_ptl:
            before = tuple(discount[d] for d in self.DAY_NAME_MAP.values())
            self._recompute_day_booleans(discount)
            if tuple(discount[d] for d in self.DAY_NAME_MAP.values()) != before:
                dow_changed |= discount

        # Push only what changed to the inventory service
        changed = to_publish | to_unpublish | dow_changed
        if changed:
            self.env['mint.ptl.day']._push_discounts_to_redis(changed.ids)

        # Say plainly whether the backlog is drained. A run that hits its cap
        # leaves work behind, and the next scheduled run picks it up.
        outstanding = (self.search_count(publish_domain)
                       + self.search_count(unpublish_domain))
        if outstanding:
            _logger.warning(
                'PTL lifecycle: %d records still outstanding after this batch '
                '(batch=%d) — will continue on the next run', outstanding, batch)

        _logger.info(
            'PTL lifecycle cron: %d published, %d unpublished, %d dow-changed '
            '(of %d active), %d pushed, %d outstanding',
            len(to_publish), len(to_unpublish), len(dow_changed),
            len(active_ptl), len(changed), outstanding,
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
