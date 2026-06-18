"""Dutchie discount publishing for vendor deal submissions.

Trigger: the existing final-approval transition — ``action_convert_to_deal``
on ``mint.deal.submission`` (state -> scheduled, which already sits behind
submission approval AND the national-promo plot gate). After the PTL deal is
built, the submission's structured targeting is transformed into a Dutchie
Backoffice discount payload and handed to the inventory service:

    POST {dutchie.publish.url}/api/admin/discounts   {locId, lspId, discount}

Modes (ir.config_parameter ``dutchie.publish.mode``):
    off      — do nothing.
    dry_run  — build the payload(s) and post them to the submission chatter;
               NO network write. This is the default.
    live     — POST per target LocId (requires ``dutchie.publish.api_key``).

Safety: ExternalId is always ``lgm_<submission_id>`` (round-trip identity +
the lgm-prefix rule for records we create in Dutchie). Publish failures never
roll back the conversion — they are reported on chatter only.

Value conventions follow docs/mappings/discount-canonical.json:
PERCENT_OFF (calc 2) DiscountValue is a 0-1 fraction (50% -> 0.5);
bundle "N for $X" is calc 6 with DiscountValue=X and ThresholdMin=N.
"""
import json
import logging
import re
import urllib.request
from datetime import date as _date, timedelta

from odoo import api, fields, models
from odoo.exceptions import UserError

from .deal_mixins import format_bundle_tiers_text, build_dutchie_restrictions

_logger = logging.getLogger(__name__)

# mint.region name -> Dutchie LSP (tenant). BrandIds/ProductIds are LSP-scoped.
LSP_BY_REGION = {
    'arizona': 575,
    'michigan': 576,
    'missouri': 723,
    'illinois': 805,
    'nevada': 820,
    'florida': 821,
}

# submission discount_type -> Dutchie CalculationMethodId (canonical registry)
CALC_BY_TYPE = {
    'percent': 2,
    'fixed': 1,
    'price': 3,
    'bundle': 6,
    'points_multiplier': 15,
    # Clearance (near-expiry) publishes as a straight PERCENT_OFF — it carries
    # a percent value like 'percent' (_format_sales_details renders
    # "N% Off (Clearance)"). Without this, _dutchie_build raised "Unsupported
    # discount type 'clearance'" — swallowed by the convert try/except — so
    # clearance deals silently never reached Dutchie.
    'clearance': 2,
}

DAY_KEYS = ('Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday')

# Vendor free-text that means "no exclusion", not a product term.
NOISE = {'', 'none', 'none.', 'na', 'n/a', 'no', 'nope', 'yes', 'all', 'all skus', '-'}
NOISE_PREFIXES = ('all ', 'everything', 'no sku', "no sku's", 'n/a')

RE_PCT = re.compile(r'(\d+(?:\.\d+)?)\s*%\s*off', re.I)
RE_NFOR = re.compile(r'(\d+)\s*for\s*\$\s*(\d+(?:\.\d+)?)', re.I)
RE_DOLLAR = re.compile(r'\$\s*(\d+(?:\.\d+)?)\s*off', re.I)
RE_BOGO = re.compile(r'\bbogo\b|\bb1g1\b|buy\s+one,?\s+get\s+one', re.I)


class DealSubmissionDutchiePublish(models.Model):
    _inherit = 'mint.deal.submission'

    dutchie_publish_loc_ids = fields.Text(
        string='Dutchie Published LocId→DiscountId (JSON)',
        copy=False,
        help='Per-LocId Dutchie discount id from the last successful live '
             'publish — JSON {"<locId>": <dutchieDiscountId>}. Each loc has '
             'its OWN Dutchie discount, so re-publish UPDATES the recorded id '
             'in place instead of creating a duplicate (the discount payload '
             'otherwise hardcodes Id=0 = always-create).',
    )

    # ------------------------------------------------------------------
    # #2 — discount value parsing (regex + reviewer gate)
    # ------------------------------------------------------------------

    @api.model
    def _parse_discount_from_text(self, text):
        """Parse ONE unambiguous offer out of vendor prose, or None.

        Fills only when the text contains exactly one kind of offer with one
        value ("50% off ...", "2 for $18", "$10 off", "BOGO"). Multi-deal
        blobs (different percentages per day, mixed bundles) return None and
        stay a reviewer task — guessing wrong on price is worse than manual.
        """
        text = text or ''
        pcts = {float(v) for v in RE_PCT.findall(text)}
        nfors = {(int(n), float(v)) for n, v in RE_NFOR.findall(text)}
        dollars = {float(v) for v in RE_DOLLAR.findall(text)}
        bogo = bool(RE_BOGO.search(text))
        kinds = sum(1 for x in (pcts, nfors, dollars, bogo) if x)
        if kinds != 1:
            return None
        if pcts:
            if len(pcts) != 1:
                return None
            return {'discount_type': 'percent', 'discount_value': pcts.pop()}
        if nfors:
            if len(nfors) != 1:
                return None
            n, amount = nfors.pop()
            return {'discount_type': 'bundle', 'discount_value': amount,
                    'threshold_min': n}
        if dollars:
            if len(dollars) != 1:
                return None
            return {'discount_type': 'fixed', 'discount_value': dollars.pop()}
        return {'discount_type': 'bogo', 'discount_value': 0.0}

    @api.model_create_multi
    def create(self, vals_list):
        # Auto-fill discount_value from the prose when the submission arrives
        # without one (JotForm imports, sparse web-form entries). Only fills a
        # blank — never overwrites reviewer input.
        for vals in vals_list:
            if not vals.get('discount_value') and vals.get('sales_details'):
                parsed = self._parse_discount_from_text(vals['sales_details'])
                # Fill only when the parsed offer kind matches the incoming
                # type (or none was given) — no cross-type value pollution.
                if parsed and vals.get('discount_type') in (None, False, '', parsed['discount_type']):
                    vals['discount_type'] = parsed['discount_type']
                    vals['discount_value'] = parsed['discount_value']
        return super().create(vals_list)

    def action_parse_discount(self):
        """Manual re-parse (list/form server action) — fills blanks only."""
        filled = 0
        for sub in self:
            if sub.discount_value:
                continue
            parsed = self._parse_discount_from_text(sub.sales_details)
            if parsed and (not sub.discount_type
                           or parsed['discount_type'] == sub.discount_type):
                sub.write({
                    'discount_type': sub.discount_type or parsed['discount_type'],
                    'discount_value': parsed['discount_value'],
                })
                filled += 1
        return filled

    # ------------------------------------------------------------------
    # #1 — publish on the existing final-approval transition
    # ------------------------------------------------------------------

    def action_convert_to_deal(self):
        self.ensure_one()
        mode = (self.env['ir.config_parameter'].sudo()
                .get_param('dutchie.publish.mode') or 'dry_run').strip().lower()
        # #2 reviewer gate: a deal can't convert (and thus can't publish) with
        # no numeric value. Exemptions: bogo (valid without one), structured
        # bundles (#93677 — pricing lives in bundle_tier_ids, and the
        # auto-generated multi-tier text defeats the RE_NFOR parse), and
        # mode=off (publishing disabled — don't block legacy conversions).
        # NOTE: clearance is NOT exempt — it maps to PERCENT_OFF and needs its
        # percent, so a value-less clearance is caught here with a clear
        # message instead of publishing a 0%-off discount.
        if (mode != 'off' and not self.discount_value
                and self.discount_type != 'bogo'
                and not (self.discount_type == 'bundle' and self.bundle_tier_ids)):
            parsed = self._parse_discount_from_text(self.sales_details)
            # Only trust the parse when its offer kind matches the chosen
            # type (or no type was chosen) — never turn "$10 off" prose into
            # a 10% discount because discount_type said percent.
            if parsed and (not self.discount_type
                           or parsed['discount_type'] == self.discount_type):
                self.write({'discount_value': parsed['discount_value'],
                            'discount_type': self.discount_type or parsed['discount_type']})
            else:
                raise UserError(
                    "Discount Value is not set and the Sales Details text is "
                    "ambiguous (multiple offers, or its offer kind doesn't "
                    "match the chosen Discount Type). Enter the value/type "
                    "for the deal being approved, then convert again."
                )
        res = super().action_convert_to_deal()
        try:
            self._dutchie_publish_after_convert()
        except Exception as exc:  # never break conversion on publish issues
            _logger.exception("Dutchie publish failed for submission %s", self.id)
            try:
                self.message_post(
                    body=f"Dutchie publish FAILED (conversion unaffected): {exc}",
                    message_type='comment',
                )
            except Exception:
                # Chatter itself can crash (rogue automation on message_post);
                # the log line above is the fallback record.
                _logger.exception("…and the failure chatter post also failed (submission %s)", self.id)
        return res

    def action_publish_to_dutchie(self):
        """Manual (re)publish for an already-converted submission.

        Auto-publish on convert never blocks the conversion, so a transient or
        data error leaves the PTL deal created but nothing in Dutchie. This
        button re-fires the publish. Unlike the auto path, errors surface
        DIRECTLY to the reviewer (normal popup) so they can fix and retry.
        """
        self.ensure_one()
        if not self.deal_id:
            raise UserError(
                "This submission hasn't been converted to a PTL deal yet — "
                "Schedule it first (publishing fires automatically on convert)."
            )
        mode = (self.env['ir.config_parameter'].sudo()
                .get_param('dutchie.publish.mode') or 'dry_run').strip().lower()
        if mode == 'off':
            raise UserError("Dutchie publishing is turned off (dutchie.publish.mode=off).")
        # Surfaces hard failures (no LSP / no restrictions / BOGO guard) as a
        # popup; posts the payload (dry_run) or per-store HTTP result (live) to
        # chatter either way.
        self._dutchie_publish_after_convert()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'type': 'success',
                'title': "Dutchie publish",
                'message': ("Dry-run payload posted to the chatter."
                            if mode == 'dry_run'
                            else "Publish attempted — see the chatter for the per-store result."),
                'sticky': False,
            },
        }

    def action_open_dutchie_publish_review(self):
        """Open the pre-publish review wizard (guards + alerts) for the
        submission Publish-to-Dutchie button. Warn-only: shows mode, target
        LocIds and SOP/safety alerts; action_publish_to_dutchie fires on
        confirm."""
        self.ensure_one()
        from .dutchie_publish_review import build_review_html
        mode, is_live, lines, warnings, blocks, requires_ack = self._dutchie_publish_review_data()
        wiz = self.env['mint.dutchie.publish.review'].create({
            'res_model': self._name,
            'res_id': self.id,
            'mode': mode,
            'is_live': is_live,
            'requires_ack': requires_ack,
            'review_html': build_review_html(mode, is_live, lines, warnings, blocks),
        })
        return {
            'type': 'ir.actions.act_window',
            'name': 'Review before publishing',
            'res_model': 'mint.dutchie.publish.review',
            'res_id': wiz.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def _dutchie_publish_review_data(self):
        """Compute (mode, is_live, lines, warnings, blocks, requires_ack) for
        the submission Dutchie publish WITHOUT writing anything (used by the
        review wizard). ``requires_ack`` is True when this deal overlaps an
        active deal's Dutchie scope — the wizard then forces an explicit
        acknowledgement before it will publish."""
        self.ensure_one()
        get_param = self.env['ir.config_parameter'].sudo().get_param
        mode = (get_param('dutchie.publish.mode') or 'dry_run').strip().lower()
        is_live = mode == 'live'
        lines, warnings, blocks = [], [], []
        if mode == 'off':
            warnings.append("dutchie.publish.mode is 'off' — publishing is disabled; confirming does nothing.")
        elif mode not in ('dry_run', 'live'):
            warnings.append("Unrecognized publish mode %r — this path treats anything that isn't 'off'/'dry_run' as LIVE (fail-open). Fix dutchie.publish.mode." % mode)
            is_live = True
        try:
            built = self._dutchie_build()
            discounts, lsp = built['discounts'], built['lsp']
            warnings.extend(built['warnings'])
            loc_map = json.loads(get_param('dutchie.publish.loc_ids') or '{}')
            loc_ids = loc_map.get(str(lsp)) or []
            lines.append("Vendor: %s — %s" % (
                self.vendor_name, discounts[0].get('OnlineName') if discounts else '?'))
            if len(discounts) > 1:
                lines.append("Schedule has gap(s) → %d SEPARATE Dutchie discounts (one per contiguous span):"
                             % len(discounts))
            for gi, discount in enumerate(discounts, 1):
                prefix = ("  • span %d: " % gi) if len(discounts) > 1 else "Dates: "
                active = [d for d in DAY_KEYS if discount.get(d)]
                lines.append("%s%s → %s  [%s]" % (
                    prefix, discount.get('ValidDateFrom'), discount.get('ValidDateTo'),
                    ', '.join(active) if active else 'EVERY DAY (no day restriction)'))
                if not active:
                    warnings.append("Span %d has no day-of-week restriction — Dutchie applies it EVERY day in the range." % gi)
            lines.append("LSP %s → %d LocId(s): %s" % (lsp, len(loc_ids), ', '.join(map(str, loc_ids)) or 'NONE'))
            lines.append("Total Dutchie writes: %d discount(s) × %d loc(s) = %d"
                         % (len(discounts), len(loc_ids), len(discounts) * len(loc_ids)))
            if not loc_ids:
                warnings.append("No LocIds configured for LSP %s (dutchie.publish.loc_ids) — nothing will publish." % lsp)
            if self.store_ids:
                warnings.append("This submission is scoped to %d store(s), but Publish-to-Dutchie writes to ALL %d LocId(s) for the market — store scoping is IGNORED on this path."
                                % (len(self.store_ids), len(loc_ids)))
            try:
                prior = json.loads(self.dutchie_publish_loc_ids or '{}')
            except (ValueError, TypeError):
                prior = {}
            if prior:
                prior_count = (sum(len(v) for v in prior.values())
                               if all(isinstance(v, dict) for v in prior.values())
                               else len(prior))
                lines.append("Re-publish: %d prior Dutchie id(s) recorded → matching span/loc UPDATE in place (no duplicates)." % prior_count)
        except Exception as exc:
            blocks.append("Payload build failed — Publish would raise: %s" % exc)
        requires_ack = False
        try:
            overlaps = self._dutchie_overlap_alerts()
            if overlaps:
                warnings.extend(overlaps)
                requires_ack = True
        except Exception as exc:  # the guard must never break the review itself
            _logger.warning("Overlap guard failed for submission %s: %s", self.id, exc)
        return mode, is_live, lines, warnings, blocks, requires_ack

    # ------------------------------------------------------------------
    # Payload assembly
    # ------------------------------------------------------------------

    def _dutchie_lsp(self):
        self.ensure_one()
        name = (self.market_id.name or '').strip().lower() if self.market_id else ''
        for key, lsp in LSP_BY_REGION.items():
            if key in name:
                return lsp
        return None

    def _dutchie_brands(self):
        """Brand records this deal targets — ID-ONLY: the explicitly chosen
        ``brand_id``/``brand_ids``. Vendor-name/alias resolution is purged;
        a deal with no brand selected resolves to no brand (and must rely on
        product/category ids, or it hard-blocks downstream)."""
        self.ensure_one()
        return (self.brand_ids | self.brand_id) if self.brand_id else self.brand_ids

    def _dutchie_scope_ids(self, lsp):
        """Resolve this deal's Dutchie targeting to
        ``(brand_ids, category_ids, product_ids)`` as frozensets, mirroring
        ``_dutchie_build``'s Brand/Category/Product restriction resolution.
        Read-only and schedule-free — used by the overlap guard to compare two
        deals' product scope without building a full payload."""
        self.ensure_one()
        brand_ids = set()
        for b in self._dutchie_brands():
            bid = b.dutchie_brand_id_for_lsp(lsp)
            if bid and str(bid).isdigit():
                brand_ids.add(int(bid))
        prod_ids = {int(p.dutchie_product_id) for p in self.product_ids
                    if str(p.dutchie_product_id or '').strip().isdigit()}
        # ID-ONLY: categories come from structured picks (real product.category
        # records carrying a dutchie_category_id). Free-text product_category
        # name matching is purged — a free-text-only deal contributes no
        # category here (and hard-blocks at publish time).
        cat_ids = set()
        picked = self._all_picked_categories()
        if picked:
            cat_ids = {int(c.dutchie_category_id) for c in picked
                       if str(c.dutchie_category_id or '').strip().isdigit()}
        return frozenset(brand_ids), frozenset(cat_ids), frozenset(prod_ids)

    def _dutchie_overlap_alerts(self):
        """Detect OTHER active Dutchie publications in the same market whose
        product scope overlaps this deal's. A publish here would mint a SECOND
        Dutchie discount competing on the same items (the publisher de-dupes
        only WITHIN a submission's own ExternalId, never across deals). Same
        brand AND (shared category, shared product, or either side targeting
        the whole brand) = overlap. Returns human-readable alert strings."""
        self.ensure_one()
        lsp = self._dutchie_lsp()
        if not lsp or not self.market_id:
            return []
        my_brands, my_cats, my_prods = self._dutchie_scope_ids(lsp)
        if not my_brands:
            return []  # no brand anchor → can't judge overlap reliably
        candidates = self.env['mint.deal.submission'].search([
            ('id', '!=', self.id),
            ('market_id', '=', self.market_id.id),
            ('state', 'not in', ('expired', 'rejected', 'new', 'under_review')),
            ('dutchie_publish_loc_ids', '!=', False),
        ])
        alerts = []
        for other in candidates:
            try:
                o_brands, o_cats, o_prods = other._dutchie_scope_ids(lsp)
            except Exception:  # a malformed sibling must never block this review
                continue
            if not (my_brands & o_brands):
                continue  # different brand → no overlap
            my_broad = not my_cats and not my_prods
            o_broad = not o_cats and not o_prods
            shared_cats = my_cats & o_cats
            shared_prods = my_prods & o_prods
            if my_broad or o_broad:
                why = "the entire brand (one side has no category/product narrowing)"
            elif shared_cats:
                why = "shared categor%s %s" % (
                    'ies' if len(shared_cats) > 1 else 'y', sorted(shared_cats))
            elif shared_prods:
                why = "shared product id(s) %s" % sorted(shared_prods)
            elif (my_cats and o_prods) or (my_prods and o_cats):
                why = "the same brand with mixed category/product scope (likely overlap)"
            else:
                continue
            alerts.append(
                "⚠ Overlaps active deal %r (submission #%d, %s) on %s — publishing "
                "will create a SECOND Dutchie discount competing on the same items."
                % (other.name or other.vendor_name or '?', other.id, other.state, why))
        return alerts

    def _exclusion_terms(self):
        """Clean vendor exclusion prose into searchable product terms (#4)."""
        terms = []
        for raw in re.split(r'[\n,;]+', self.excluded_skus or ''):
            t = raw.strip()
            low = t.lower()
            if not t or low in NOISE or any(low.startswith(p) for p in NOISE_PREFIXES):
                continue
            # strip directive lead-ins: "No ...", "Exclude/Excluding ...",
            # "This deal excludes ..."
            t = re.sub(r'^(this\s+deal\s+excludes?|excludes?|excluding|no)\s+',
                       '', t, flags=re.I).strip(' .!')
            if t:
                terms.append(t)
        return terms

    @staticmethod
    def _dutchie_date(d, end_of_day=False):
        return f"{d.month}/{d.day}/{d.year}, " + ("11:59:59 PM" if end_of_day else "12:00:00 AM")

    @staticmethod
    def _partition_faithful(dates):
        """Partition sorted, unique date objects into 'faithful' groups.

        A group is faithful when collapsing it to [min, max] + the union of its
        weekdays reproduces EXACTLY the group's dates — no date inside the span
        that matches one of the group's weekdays is missing from it. Greedy
        left-to-right: extend the current group while it stays faithful, else
        start a new one. An unbroken weekly recurrence (every Thu/Fri/Sat for
        12 weeks) stays ONE group; a real calendar gap forces a split so the
        published Dutchie discount never fires on un-plotted days between
        windows. Returns a list of date-object lists (each already sorted)."""
        groups = []
        cur = []
        for d in dates:
            if not cur:
                cur = [d]
                continue
            allowed = set(cur) | {d}
            dows = {x.weekday() for x in allowed}
            probe, ok = cur[0], True
            while probe <= d:
                if probe.weekday() in dows and probe not in allowed:
                    ok = False
                    break
                probe += timedelta(days=1)
            if ok:
                cur.append(d)
            else:
                groups.append(cur)
                cur = [d]
        if cur:
            groups.append(cur)
        return groups

    def _dutchie_build(self, for_deactivation=False):
        """Build {lsp, discount, warnings} for this submission, or raise.

        ``for_deactivation``: when True (called from _dutchie_deactivate), the
        ID-only targeting guards do NOT hard-block — restrictions are
        irrelevant when re-POSTing with IsDeleted=True, and a legacy
        fuzzy-targeted deal must still be deactivatable. Unresolvable
        targeting is best-effort/skipped instead of raising.
        """
        self.ensure_one()
        warnings = []
        lsp = self._dutchie_lsp()
        if not lsp:
            raise UserError(f"No Dutchie LSP mapping for market {self.market_id.name!r}.")

        # ── ID-ONLY targeting (all fuzzy/name matching purged) ───────────
        # Every restriction is resolved from explicit identifiers: brand →
        # per-LSP dutchie id, products → dutchie_product_id, categories →
        # dutchie_category_id (structured picks), excluded brands → id. Any
        # targeting that can't resolve to an id HARD-BLOCKS the publish — we
        # never name-match and never silently drop a restriction (dropping one
        # would publish a broader / duplicate discount).
        brands = self._dutchie_brands()
        brand_ids = []
        for b in brands:
            bid = b.dutchie_brand_id_for_lsp(lsp)
            if bid and str(bid).isdigit():
                brand_ids.append(int(bid))
            elif not for_deactivation:
                raise UserError(
                    f"Brand {b.name!r} has no Dutchie brand id for LSP {lsp}. "
                    f"Add it on the brand (mint.brand.dutchie_brand_ids) — "
                    f"name/alias matching is disabled.")

        unres_inc = [p.name for p in self.product_ids
                     if not str(p.dutchie_product_id or '').strip().isdigit()]
        if unres_inc and not for_deactivation:
            raise UserError(
                "These products have no Dutchie product id: "
                + ", ".join(unres_inc[:10]) + (" …" if len(unres_inc) > 10 else "")
                + ". Resolve their dutchie_product_id or remove them — "
                "name matching is disabled.")
        prod_inc = [int(p.dutchie_product_id) for p in self.product_ids
                    if str(p.dutchie_product_id or '').strip().isdigit()]

        # Exclusions: brand ids only. Free-text excluded_skus can't be
        # id-matched — block rather than silently widen the discount.
        exc_terms = self._exclusion_terms()
        if exc_terms and not for_deactivation:
            raise UserError(
                "Excluded SKUs are free text (%s) — SKU/name exclusion matching "
                "is disabled. Use Excluded Brands, add the products to exclude "
                "by their Dutchie ids, or clear the field."
                % ", ".join(exc_terms[:5]))
        prod_exc = []
        exc_brand_ids = []
        for b in self.excluded_brand_ids:
            bid = b.dutchie_brand_id_for_lsp(lsp)
            if bid and str(bid).isdigit():
                exc_brand_ids.append(int(bid))
            elif not for_deactivation:
                raise UserError(
                    f"Excluded brand {b.name!r} has no Dutchie id for LSP {lsp}.")

        # Category restriction — structured picks ONLY. The In-Stock Categories
        # dropdown stores real product.category records; emit their Dutchie
        # ids. Free-text product_category name matching is purged.
        cat_ids = []
        picked = self._all_picked_categories()
        if picked:
            unres = [c.name for c in picked
                     if not str(c.dutchie_category_id or '').strip().isdigit()]
            if unres and not for_deactivation:
                raise UserError(
                    "These picked categories have no Dutchie category id: "
                    + ", ".join(unres) + ".")
            cat_ids = sorted({int(c.dutchie_category_id) for c in picked
                              if str(c.dutchie_category_id or '').strip().isdigit()})
        elif self.product_category and self.product_category.strip().lower() not in NOISE \
                and not for_deactivation:
            raise UserError(
                "Category %r is set as free text, not structured picks — "
                "free-text category matching is disabled. Choose categories "
                "from the In-Stock Categories dropdown." % self.product_category)

        # Active window + day-of-week from the structured plot windows.
        # all_dates() returns ISO strings — coerce to date objects.
        dates = [_date.fromisoformat(d) if isinstance(d, str) else d
                 for d in (self.window_ids.all_dates() if self.window_ids else [])]
        if not dates and self.preferred_start_date:
            cur = self.preferred_start_date
            end = self.preferred_end_date or cur
            while cur <= end:
                dates.append(cur)
                cur += timedelta(days=1)
        if not dates:
            raise UserError("No schedule: add Plot Windows (or preferred dates) before converting.")
        dates = sorted(set(dates))
        # Split into FAITHFUL groups so a non-contiguous schedule (e.g. early
        # July + late September, skipping August) becomes SEPARATE Dutchie
        # discounts — collapsing every plotted date to a single [min, max]
        # range + weekday union would also activate the discount on matching
        # weekdays inside the gap, days that were never scheduled. An unbroken
        # weekly recurrence stays a SINGLE group. Day-of-week flags are derived
        # per group below.
        groups = self._partition_faithful(dates)

        # BOGO: Dutchie has no native BOGO type. The live-verified encoding
        # (discounts 379870 "BOGO 50%" / 379191 "BOGO" at Tempe, read raw
        # 2026-06-12) is PERCENT_OFF + NUMBER_OF_ITEMS threshold 2 +
        # ApplyToOnlyOneItem=True: buy two, the discount hits ONE of them.
        # discount_value carries the percent (50 = "BOGO 50% off");
        # empty/0 means the second item is free (100%).
        is_bogo = self.discount_type == 'bogo'
        calc = 2 if is_bogo else CALC_BY_TYPE.get(self.discount_type)
        if not calc:
            raise UserError(f"Unsupported discount type {self.discount_type!r} for Dutchie publish.")
        value = float(self.discount_value or 0)
        threshold_min = None
        apply_to_one = False
        if is_bogo:
            # Guard: type=bogo with a blank value but percent prose ("BOGO
            # 50% off ...") would silently publish a FREE item. Make the
            # reviewer set the value explicitly.
            if not value and RE_PCT.search(self.sales_details or ''):
                raise UserError(
                    "This BOGO's Sales Details mention a percentage but "
                    "Discount Value is empty — set it (e.g. 50 for 'BOGO "
                    "50% off') or it would publish as a FREE item."
                )
            value = (value / 100.0) if value else 1.0
            threshold_min = 2
            apply_to_one = True
        elif calc == 2:
            # Model convention is WHOLE percents (50 = 50% — see
            # _format_sales_details). Divide unconditionally: a stored 1
            # means 1% (0.01), never 100%.
            value = value / 100.0
        if calc == 2 and value > 1.0:
            raise UserError(
                f"Discount value {value * 100:g}% exceeds 100% — refusing to publish."
            )
        elif calc == 6:
            if self.bundle_tier_ids:
                # Structured tiers (#93677) are the source of truth: first
                # tier's qty/price. Dutchie's calc-6 reward holds a single
                # (threshold, total) pair, so extra tiers need their own
                # Backoffice discount — surface that instead of dropping
                # them silently.
                first = self.bundle_tier_ids[0]
                threshold_min = first.qty
                value = float(first.price)
                if len(self.bundle_tier_ids) > 1:
                    extra = format_bundle_tiers_text(
                        [(t.qty, t.price) for t in self.bundle_tier_ids[1:]])
                    warnings.append(
                        f"only the first bundle tier was published; build the "
                        f"remaining tier(s) ({extra}) manually in Backoffice")
            else:
                m = RE_NFOR.search(self.sales_details or '')
                threshold_min = int(m.group(1)) if m else 2

        # Brand/Product/Category restriction assembly lives in a pure helper so
        # it is unit-testable in isolation (see tests/test_dutchie_restrictions).
        # The Product include is deliberately dropped when a Brand/Category
        # already scopes the deal — Dutchie AND's restriction types, so layering
        # would only shrink eligibility (sub 395: 233 → 48).
        restrictions, _restr_warns = build_dutchie_restrictions(
            brand_ids, exc_brand_ids, prod_inc, prod_exc, cat_ids)
        warnings += _restr_warns

        # Refuse a discount with NO restrictions at all — that would apply
        # store-wide (every product, every brand). Reachable when none of the
        # deal's brands resolve to a per-LSP Dutchie id and there are no
        # product/category restrictions either. Skipped for deactivation: an
        # empty-restriction payload is fine when re-POSTing IsDeleted=True
        # (Dutchie matches the delete by Id/ExternalId, not restrictions), and
        # a legacy deal that only ever resolved via the removed fuzzy paths
        # must still be deactivatable.
        if not for_deactivation and not any(r['RestrictionIds'] for r in restrictions.values()):
            raise UserError(
                "Refusing to publish: no Brand/Product/Category restriction "
                "resolved — the discount would apply store-wide. "
                + ("; ".join(warnings) or "")
            )

        label = (("BOGO" if value >= 1.0 else f"BOGO {value * 100:g}% Off") if is_bogo
                 else f"{value * 100:g}% Off" if calc == 2
                 else f"{threshold_min} for ${value:g}" if calc == 6
                 else f"${value:g} Off" if calc == 1
                 else f"${value:g}")
        # One Dutchie discount per faithful date-group. All non-date fields are
        # shared; only ValidDate*, the day-of-week flags, and ExternalId vary.
        # A single group keeps the historic un-suffixed ExternalId (lgm_<id>)
        # so already-published deals UPDATE in place; multiple groups get
        # lgm_<id>_w<k> so each gap-separated span is its own Dutchie record.
        n_groups = len(groups)
        discounts = []
        for gidx, grp in enumerate(groups, 1):
            gdows = {d.weekday() for d in grp}
            # python weekday Mon=0; DAY_KEYS starts Sunday (idx 0 -> py 6).
            day_flags = {key: ((i - 1) % 7) in gdows
                         for i, key in enumerate(DAY_KEYS)}
            external_id = (f"lgm_{self.id}" if n_groups == 1
                           else f"lgm_{self.id}_w{gidx}")
            discounts.append({
                'Id': 0,
                'ApplicationMethodId': 1,
                'CanStackAutomatically': False,
                'Constraints': [],
                'DiscountDescription': f"lgm | {self.vendor_name} (Odoo sub {self.id})",
                'ExternalId': external_id,
                'FirstTimeCustomerOnly': 0,
                'IgnoreNetTax': False,
                'IsAvailableOnline': True,
                'IsBundledDiscount': calc == 6,
                'LocationRestrictions': [],
                'OnlineName': f"{self.vendor_name} — {label}",
                'PaymentRestrictions': {'PayByBankSignupIncentive': False},
                'RedemptionLimit': '',
                'RequireManagerApproval': False,
                'RestrictToGroupIds': [],
                'RestrictToSegmentIds': [],
                'PlatformTypeRestrictions': [{'PlatformTypeId': 2, 'IsExclusion': False}],
                'OrderTypeRestrictions': [],
                'Reward': {
                    'DiscountRewardId': None,
                    'HasThreshold': bool(threshold_min) or calc in (5, 6),
                    'ApplyToOnlyOneItem': apply_to_one,
                    'CalculationMethodId': calc,
                    'DiscountValue': value,
                    'IncludeNonCannabis': False,
                    # 5 = single-item discount, 6 = bundle grouping (see
                    # dutchie_discount_push.py docs). Live BOGO records use 5.
                    'ItemGroupTypeId': 5 if is_bogo else 6,
                    'ManualDefaultApplyTo': 1,
                    'Restrictions': restrictions,
                    'ThresholdMax': None,
                    'ThresholdMin': threshold_min,
                    'ThresholdTypeId': 1 if threshold_min else 2 if calc == 5 else 0,
                },
                'SavedWithAdvancedOptions': False,
                'ValidDateFrom': self._dutchie_date(grp[0]),
                'ValidDateTo': self._dutchie_date(grp[-1], end_of_day=True),
                'DiscountCode': '',
                'MaxRedemptions': 0,
                'RedemptionLimitCountingMode': 0,
                **day_flags,
                'MenuDisplayRank': 0,
                'DiscountMenuDisplayDetails': {
                    'DiscountId': 0,
                    'MenuDisplayImageUrl': '',
                    'MenuDisplayName': self.vendor_name or '',
                    'MenuDisplayDescription': '',
                    'DiscountMenuDisplayId': None,
                },
            })
        return {'lsp': lsp, 'discounts': discounts, 'warnings': warnings}

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def _deal_audit_log(self, action, lsp, loc_id, discount, **extra):
        """Structured deal-audit line. Ships to Grafana Loki via
        mint_loki_logger (any _logger.info on this instance is forwarded),
        greppable in Railway logs either way. One line per deal x store
        action so LogQL can count/filter on the JSON fields."""
        rew = discount.get('Reward') or {}
        restr = rew.get('Restrictions') or {}
        entry = {
            'event': 'deal.audit',
            'action': action,
            'submission_id': self.id,
            'external_id': discount.get('ExternalId'),
            'name': discount.get('OnlineName') or discount.get('DiscountDescription'),
            'lsp_id': lsp,
            'loc_id': loc_id,
            'calc_method_id': rew.get('CalculationMethodId'),
            'value': rew.get('DiscountValue'),
            'valid_from': discount.get('ValidDateFrom'),
            'valid_to': discount.get('ValidDateTo'),
            'brand_ids': (restr.get('Brand') or {}).get('RestrictionIds') or [],
            'category_ids': (restr.get('Category') or {}).get('RestrictionIds') or [],
            'user_id': self.env.uid,
            'user': self.env.user.login,
        }
        entry.update(extra)
        _logger.info("deal.audit %s", json.dumps(entry, default=str))

    def _dutchie_publish_after_convert(self):
        self.ensure_one()
        get_param = self.env['ir.config_parameter'].sudo().get_param
        mode = (get_param('dutchie.publish.mode') or 'dry_run').strip().lower()
        if mode == 'off':
            return
        built = self._dutchie_build()
        lsp, discounts, warnings = built['lsp'], built['discounts'], built['warnings']
        loc_map = json.loads(get_param('dutchie.publish.loc_ids') or '{}')
        loc_ids = loc_map.get(str(lsp)) or []
        # Overlap guard (auto-convert path can't prompt for an override, so it
        # only annotates the chatter — the interactive Publish button enforces
        # the acknowledgement). Never let the guard break a conversion.
        try:
            warnings = list(warnings) + self._dutchie_overlap_alerts()
        except Exception as exc:
            _logger.warning("Overlap guard failed for submission %s: %s", self.id, exc)

        if mode == 'dry_run':
            spans = "\n".join(
                f"  span {i}: {d['ValidDateFrom']} → {d['ValidDateTo']} "
                f"[{', '.join(k for k in DAY_KEYS if d.get(k)) or 'EVERY DAY'}] "
                f"ExternalId={d['ExternalId']}"
                for i, d in enumerate(discounts, 1))
            body = (
                f"[Dutchie publish — DRY RUN]\n"
                f"LSP {lsp} | {len(discounts)} discount(s) × LocIds {loc_ids or '(none configured)'}\n"
                f"{spans}\n"
                f"Warnings: {'; '.join(warnings) or 'none'}\n"
                f"Payload (span 1):\n{json.dumps(discounts[0], indent=1) if discounts else '(none)'}"
            )
            self.message_post(body=body, message_type='comment')
            for d in discounts:
                self._deal_audit_log('publish_dry_run', lsp, None, d,
                                     target_loc_ids=loc_ids)
            return

        # live
        url = (get_param('dutchie.publish.url')
               or 'https://mintinvsvc-production-6aa5.up.railway.app').rstrip('/')
        api_key = get_param('dutchie.publish.api_key')
        if not api_key:
            raise UserError("dutchie.publish.api_key is not configured for live mode.")
        if not loc_ids:
            raise UserError(f"dutchie.publish.loc_ids has no LocIds for LSP {lsp}.")
        # Resolve update-vs-create PER (span, LocId). Each contiguous span is a
        # distinct Dutchie discount keyed by its ExternalId; each loc within a
        # span has its OWN Dutchie id. Re-publish UPDATES every (span, loc) in
        # place instead of duplicating. Recorded map is
        # {externalId: {locId: dutchieId}}; legacy single-discount deals stored
        # a flat {locId: dutchieId} — migrate those under the lone span's
        # ExternalId (lgm_<id>) so they keep updating in place.
        try:
            published = json.loads(self.dutchie_publish_loc_ids or '{}')
        except (ValueError, TypeError):
            published = {}
        if published and not all(isinstance(v, dict) for v in published.values()):
            published = {f"lgm_{self.id}": published}
        updated = {k: dict(v) for k, v in published.items()}
        # Per-(span, loc) isolation: one store failing must not hide which
        # stores DID publish — accumulate every outcome and report them all.
        results, failures = [], 0
        multi = len(discounts) > 1
        for discount in discounts:
            ext = discount['ExternalId']
            span_prior = published.get(ext, {})
            span_updated = updated.setdefault(ext, {})
            for loc_id in loc_ids:
                existing = int(span_prior.get(str(loc_id)) or 0)
                discount['Id'] = existing
                if isinstance(discount.get('DiscountMenuDisplayDetails'), dict):
                    discount['DiscountMenuDisplayDetails']['DiscountId'] = existing
                payload = json.dumps({'locId': loc_id, 'lspId': lsp, 'discount': discount}).encode()
                req = urllib.request.Request(
                    f"{url}/api/admin/discounts", data=payload,
                    headers={'Content-Type': 'application/json', 'x-api-key': api_key},
                    method='POST')
                tag = (f"{ext} LocId {loc_id}" if multi else f"LocId {loc_id}")
                try:
                    with urllib.request.urlopen(req, timeout=60) as resp:
                        raw = resp.read().decode(errors='replace')
                        # Record the returned Dutchie id so the NEXT publish
                        # updates this (span, loc) in place (no duplicates).
                        try:
                            rid = json.loads(raw).get('discount_id')
                            if isinstance(rid, int) and not isinstance(rid, bool) and rid > 0:
                                span_updated[str(loc_id)] = rid
                        except (ValueError, TypeError):
                            pass
                        results.append(f"{tag}: HTTP {resp.status} {raw[:160]}")
                        self._deal_audit_log('publish', lsp, loc_id, discount,
                                             http_status=resp.status,
                                             response=raw[:160])
                except Exception as exc:
                    failures += 1
                    results.append(f"{tag}: FAILED — {exc}")
                    self._deal_audit_log('publish_failed', lsp, loc_id, discount,
                                         error=str(exc))
        # Persist the {externalId: {locId: id}} map for idempotent re-publish.
        if updated != published:
            self.sudo().write({'dutchie_publish_loc_ids': json.dumps(updated)})
        self.message_post(
            body=f"[Dutchie publish — LIVE{' — PARTIAL FAILURE' if failures else ''}]\n"
                 + (f"{len(discounts)} span(s) × {len(loc_ids)} loc(s):\n" if multi else "")
                 + "\n".join(results)
                 + ("\nWarnings: " + "; ".join(warnings) if warnings else ''),
            message_type='comment')

    def _dutchie_deactivate(self):
        """Mark this submission's published Dutchie discounts deleted (IsDeleted=True).

        The inverse of _dutchie_publish_after_convert: expiring or revoking a
        deal in Odoo must also stop its discount in Dutchie, otherwise Dutchie
        keeps applying it until its own ValidDateTo — the publish was
        one-directional, which is why an expired deal stayed live in Dutchie.

        Only discounts we actually published are touched: the per-(span, loc)
        Dutchie ids recorded in dutchie_publish_loc_ids ({externalId:{locId:id}}).
        Nothing is created — a (span, loc) with no recorded id is skipped. We
        re-POST the SAME full payload as publish (a bare {Id, IsDeleted} body
        NREs in Dutchie's update-discount-item) with IsDeleted=True. Gated by the
        same dutchie.publish.mode param (off → no-op; dry_run → chatter only;
        live → POST). The recorded id map is left intact so a later re-publish
        UPDATES the same Dutchie record in place (reactivating it).
        """
        self.ensure_one()
        get_param = self.env['ir.config_parameter'].sudo().get_param
        mode = (get_param('dutchie.publish.mode') or 'dry_run').strip().lower()
        if mode == 'off':
            return
        try:
            published = json.loads(self.dutchie_publish_loc_ids or '{}')
        except (ValueError, TypeError):
            published = {}
        # Legacy flat {locId: id} maps live under the lone span's ExternalId.
        if published and not all(isinstance(v, dict) for v in published.values()):
            published = {f"lgm_{self.id}": published}
        if not any(isinstance(v, dict) and v for v in published.values()):
            return  # never published live → nothing in Dutchie to pull

        # Rebuild the full payload so update-discount-item gets a valid Discount
        # object. Index built spans by ExternalId; fall back to the first span as
        # a template for any recorded ExternalId the rebuild no longer produces
        # (e.g. the deal's windows changed after publish).
        # Permissive build: deactivation re-POSTs with IsDeleted=True, so the
        # ID-only targeting guards must not block pulling a legacy/fuzzy deal.
        built = self._dutchie_build(for_deactivation=True)
        lsp, discounts = built['lsp'], built['discounts']
        by_ext = {d['ExternalId']: d for d in discounts}
        template = discounts[0] if discounts else None

        url = api_key = None
        if mode == 'live':
            url = (get_param('dutchie.publish.url')
                   or 'https://mintinvsvc-production-6aa5.up.railway.app').rstrip('/')
            api_key = get_param('dutchie.publish.api_key')
            if not api_key:
                raise UserError("dutchie.publish.api_key is not configured for live mode.")

        results, failures, deleted = [], 0, 0
        for ext, loc_map in published.items():
            if not isinstance(loc_map, dict):
                continue
            discount = dict(by_ext.get(ext) or template or {})
            if not discount:
                continue
            discount['ExternalId'] = ext
            discount['IsDeleted'] = True
            for loc_id, dutchie_id in loc_map.items():
                existing = int(dutchie_id or 0)
                if not existing:
                    continue  # nothing live for this (span, loc)
                discount['Id'] = existing
                if isinstance(discount.get('DiscountMenuDisplayDetails'), dict):
                    discount['DiscountMenuDisplayDetails'] = dict(discount['DiscountMenuDisplayDetails'])
                    discount['DiscountMenuDisplayDetails']['DiscountId'] = existing
                tag = f"{ext} LocId {loc_id}"
                if mode == 'dry_run':
                    results.append(f"{tag}: [dry-run] would delete Dutchie id {existing}")
                    self._deal_audit_log('deactivate_dry_run', lsp, int(loc_id), discount)
                    continue
                payload = json.dumps({'locId': int(loc_id), 'lspId': lsp, 'discount': discount}).encode()
                req = urllib.request.Request(
                    f"{url}/api/admin/discounts", data=payload,
                    headers={'Content-Type': 'application/json', 'x-api-key': api_key},
                    method='POST')
                try:
                    with urllib.request.urlopen(req, timeout=60) as resp:
                        raw = resp.read().decode(errors='replace')
                        ok = 200 <= resp.status < 300
                        failures += 0 if ok else 1
                        deleted += 1 if ok else 0
                        results.append(f"{tag}: HTTP {resp.status} {raw[:160]}")
                        self._deal_audit_log('deactivate' if ok else 'deactivate_failed',
                                             lsp, int(loc_id), discount,
                                             http_status=resp.status, response=raw[:160])
                except Exception as exc:
                    failures += 1
                    results.append(f"{tag}: FAILED — {exc}")
                    self._deal_audit_log('deactivate_failed', lsp, int(loc_id), discount,
                                         error=str(exc))
        if results:
            verb = 'DRY RUN' if mode == 'dry_run' else ('LIVE' + (' — PARTIAL FAILURE' if failures else ''))
            self.message_post(
                body=f"[Dutchie deactivate — {verb}]\n" + "\n".join(results),
                message_type='comment')
        # Self-confirm: once every recorded (span, loc) is deleted in Dutchie,
        # clear the publish map so Odoo accurately reflects "no longer live in
        # Dutchie". This is the canonical signal the Deal Parity Monitor keys on
        # (expired/rejected submissions with a populated dutchie_publish_loc_ids
        # = still live). The publish/deactivate chatter retains the per-id audit
        # trail. Only on a fully-successful LIVE pull-down — partial failures
        # keep the map so the remaining ids can be retried.
        if mode == 'live' and deleted and not failures:
            self.sudo().write({'dutchie_publish_loc_ids': '{}'})
